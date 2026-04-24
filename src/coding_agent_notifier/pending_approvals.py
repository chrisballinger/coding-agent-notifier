"""Approval registry shared by hooks and the Slack Socket Mode daemon.

Flow:
    1. Claude Code fires `PreToolUse`. The hook calls `create(approval_id, …)`,
       which writes a JSON record and creates a named FIFO.
    2. The hook posts a Slack message with approve/deny buttons carrying the
       same `approval_id`, stashes the message `(channel, ts)` via
       `set_message_ref`, and blocks on `wait(approval_id, timeout=…)`.
    3. The daemon receives the button click, calls `resolve(approval_id,
       decision, actor=…)`. That writes the decision into the record and
       unblocks the FIFO (writer open + close is enough — the reader's
       `select` returns readable).
    4. Hook's `wait` returns the decision string; hook emits Claude Code's
       expected JSON and exits. `cleanup` unlinks the record + FIFO.

Mirrors `pending.py` for locking and serialization; diverges in that each
approval has its own `{approval_id}.json` (not keyed by session) and an
accompanying `{approval_id}.fifo`.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import select
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


def default_approvals_dir() -> Path:
    from . import paths
    return paths.approvals_dir()


def _record_path(approval_id: str, base_dir: Path | None) -> Path:
    return (base_dir or default_approvals_dir()) / f"{_safe(approval_id)}.json"


def _fifo_path(approval_id: str, base_dir: Path | None) -> Path:
    return (base_dir or default_approvals_dir()) / f"{_safe(approval_id)}.fifo"


def _lock_path(approval_id: str, base_dir: Path | None) -> Path:
    return (base_dir or default_approvals_dir()) / f"{_safe(approval_id)}.lock"


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)


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


def create(
    approval_id: str,
    *,
    agent: str,
    session_id: str | None,
    tool_name: str | None,
    tool_input: dict | None = None,
    base_dir: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> Path:
    """Write the initial record and mkfifo the wake pipe. Returns record path."""
    record = _record_path(approval_id, base_dir)
    fifo = _fifo_path(approval_id, base_dir)
    record.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "approval_id": approval_id,
        "agent": agent,
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input if isinstance(tool_input, dict) else None,
        "created_at": clock(),
        "decision": None,
        "actor": None,
        "resolved_at": None,
        "channel": None,
        "message_ts": None,
    }
    with _locked(_lock_path(approval_id, base_dir)):
        from . import paths as _paths
        _paths.write_secure(record, json.dumps(payload))
        if not fifo.exists():
            os.mkfifo(fifo, 0o600)
    return record


def set_message_ref(
    approval_id: str,
    channel: str,
    message_ts: str,
    *,
    base_dir: Path | None = None,
) -> None:
    """Remember the Slack message (channel, ts) so `resolve` can edit it."""
    record = _record_path(approval_id, base_dir)
    with _locked(_lock_path(approval_id, base_dir)):
        if not record.exists():
            return
        data = json.loads(record.read_text())
        data["channel"] = channel
        data["message_ts"] = message_ts
        from . import paths as _paths
        _paths.write_secure(record, json.dumps(data))


def resolve(
    approval_id: str,
    decision: str,
    *,
    actor: str | None = None,
    base_dir: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> dict | None:
    """Mark the approval resolved and wake the waiting hook. Returns record or None."""
    if decision not in ("allow", "deny"):
        raise ValueError(f"decision must be 'allow' or 'deny', got {decision!r}")
    record = _record_path(approval_id, base_dir)
    fifo = _fifo_path(approval_id, base_dir)
    with _locked(_lock_path(approval_id, base_dir)):
        if not record.exists():
            return None
        data = json.loads(record.read_text())
        if data.get("decision") is not None:
            # Already resolved — idempotent no-op.
            return data
        data["decision"] = decision
        data["actor"] = actor
        data["resolved_at"] = clock()
        from . import paths as _paths
        _paths.write_secure(record, json.dumps(data))
    # Wake outside the lock: opening a FIFO for write blocks until a reader
    # is open, and the reader path below also tries to lock — keeping the
    # write-open inside `_locked` would deadlock.
    _kick(fifo)
    return data


def _kick(fifo: Path) -> None:
    if not fifo.exists():
        return
    try:
        fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        # ENXIO = no reader yet. The reader will see the record on its own
        # when it next checks; our wake signal is best-effort.
        if e.errno == errno.ENXIO:
            return
        return
    try:
        try:
            os.write(fd, b"x")
        except OSError:
            pass
    finally:
        os.close(fd)


def read(approval_id: str, *, base_dir: Path | None = None) -> dict | None:
    record = _record_path(approval_id, base_dir)
    if not record.exists():
        return None
    try:
        return json.loads(record.read_text())
    except (OSError, ValueError):
        return None


def wait(
    approval_id: str,
    *,
    timeout: float,
    base_dir: Path | None = None,
    poll_interval: float = 1.0,
) -> str | None:
    """Block up to `timeout` seconds for a decision. Returns "allow"/"deny" or
    None on timeout. Polls the record file in addition to select'ing on the
    FIFO so we recover if the daemon wrote the decision before the FIFO open
    (race) or if the FIFO write was dropped (ENXIO)."""
    fifo = _fifo_path(approval_id, base_dir)
    if not fifo.exists():
        fifo.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(fifo, 0o600)
    deadline = time.monotonic() + timeout
    fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    try:
        while True:
            # Check record first — handles the resolve-before-wait race.
            rec = read(approval_id, base_dir=base_dir)
            if rec and rec.get("decision") in ("allow", "deny"):
                return rec["decision"]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], min(remaining, poll_interval))
            if ready:
                try:
                    os.read(fd, 64)
                except OSError:
                    pass
                rec = read(approval_id, base_dir=base_dir)
                if rec and rec.get("decision") in ("allow", "deny"):
                    return rec["decision"]
                # Spurious wake (writer opened and closed with no decision
                # written yet) — loop and re-check.
    finally:
        os.close(fd)


def cleanup(approval_id: str, *, base_dir: Path | None = None) -> None:
    """Remove the record, FIFO, and lock. Best-effort; swallows errors."""
    for path in (
        _record_path(approval_id, base_dir),
        _fifo_path(approval_id, base_dir),
        _lock_path(approval_id, base_dir),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def list_pending(*, base_dir: Path | None = None) -> list[dict]:
    """Scan all unresolved records. Used by `agent-notify status` and by
    the daemon on startup to recover from a crash."""
    root = base_dir or default_approvals_dir()
    if not root.exists():
        return []
    out: list[dict] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("decision") is None:
            out.append(data)
    return out


def gc_stale(*, max_age_seconds: float = 3600.0, base_dir: Path | None = None,
             clock: Callable[[], float] = time.time) -> int:
    """Remove records older than `max_age_seconds`. Returns count removed."""
    root = base_dir or default_approvals_dir()
    if not root.exists():
        return 0
    now = clock()
    removed = 0
    for path in list(root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        created = data.get("created_at") if isinstance(data, dict) else None
        if not isinstance(created, (int, float)):
            continue
        if now - created < max_age_seconds:
            continue
        aid = data.get("approval_id")
        if isinstance(aid, str):
            cleanup(aid, base_dir=base_dir)
            removed += 1
    return removed
