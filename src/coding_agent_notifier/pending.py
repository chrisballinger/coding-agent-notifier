"""File-based pending queue for deferred turn_complete dispatch.

When a `turn_complete` hook fires, we write the serialized event to a
per-session file and spawn a detached child to dispatch it after a short
coalesce window. If an `idle_prompt` for the same session arrives in the
meantime, it *claims* the pending file (atomic unlink under flock) and the
child's subsequent claim returns None, so the duplicate ping is silently
dropped.

State lives under `$XDG_CACHE_HOME/coding-agent-notifier/pending/`. Each file
is tiny (one serialized event); expired entries are GC'd lazily.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .event import Event


def default_pending_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "coding-agent-notifier" / "pending"


def _path_for(agent: str, session_id: str | None, *, base_dir: Path | None = None) -> Path:
    root = base_dir or default_pending_dir()
    # Session id may be None for Codex's legacy notify shape; collapse to "-".
    sid = session_id or "-"
    # Sanitize in case anything weird slips in — only alnum, dash, underscore.
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)
    return root / f"{agent}-{safe}.json"


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def write(event: Event, *, base_dir: Path | None = None, clock: callable = time.time) -> Path:
    """Serialize `event` to a per-session pending file. Overwrites any prior entry."""
    key_path = _path_for(event.agent, event.session_id, base_dir=base_dir)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _serialize(event, created_at=clock())
    lock_path = key_path.with_suffix(".lock")
    with _locked(lock_path):
        tmp = key_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, key_path)
    return key_path


def claim(
    agent: str,
    session_id: str | None,
    *,
    base_dir: Path | None = None,
    max_age_seconds: float = 60.0,
    clock: callable = time.time,
) -> Event | None:
    """Atomically read-and-delete the pending entry, or return None if absent/stale."""
    key_path = _path_for(agent, session_id, base_dir=base_dir)
    lock_path = key_path.with_suffix(".lock")
    with _locked(lock_path):
        if not key_path.exists():
            return None
        try:
            raw = key_path.read_text()
        except OSError:
            raw = ""
        try:
            key_path.unlink()
        except OSError:
            pass
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    created = data.get("created_at")
    if isinstance(created, (int, float)) and clock() - created > max_age_seconds:
        return None
    return _deserialize(data)


def _serialize(event: Event, *, created_at: float) -> dict:
    return {
        "agent": event.agent,
        "kind": event.kind,
        "message": event.message,
        "cwd": str(event.cwd),
        "session_id": event.session_id,
        "tool_name": event.tool_name,
        "tool_input": event.tool_input,
        "source_app": event.source_app,
        "transcript_path": str(event.transcript_path) if event.transcript_path else None,
        "created_at": created_at,
    }


def _deserialize(data: dict) -> Event | None:
    try:
        return Event(
            agent=data["agent"],
            kind=data["kind"],
            message=data.get("message") or "",
            cwd=Path(data["cwd"]),
            session_id=data.get("session_id"),
            tool_name=data.get("tool_name"),
            tool_input=data.get("tool_input") if isinstance(data.get("tool_input"), dict) else None,
            source_app=data.get("source_app"),
            transcript_path=Path(data["transcript_path"]) if data.get("transcript_path") else None,
        )
    except (KeyError, TypeError, ValueError):
        return None
