"""Short-TTL cross-process dedup.

Claude Code fires both `PermissionRequest` and `Notification:permission_prompt`
for the same approval gate, so a naive pipeline pings Slack twice per prompt.
We dedup on a stable key within a short window using a small JSON file guarded
by `fcntl.flock`.

Not suitable for long-lived state — this is just swallowing the twin-fire.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


def default_state_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "coding-agent-notifier" / "dedup.json"


@contextmanager
def _locked(path: Path) -> Iterator[int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read(fd: int) -> dict[str, float]:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        raw = os.read(fd, 1_000_000).decode("utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}


def _write(fd: int, data: dict[str, float]) -> None:
    encoded = json.dumps(data).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, encoded)


def recently_seen(
    key: str,
    *,
    ttl: float = 5.0,
    path: Path | None = None,
    clock: Callable[[], float] | None = None,
) -> bool:
    """Return True iff `key` was recorded within the last `ttl` seconds.

    Otherwise records `key` at the current time and returns False. Expired keys
    are garbage-collected on every call so the state file can't grow unbounded.

    `clock` defaults to `time.monotonic`, but is resolved dynamically rather
    than bound at function-definition time so tests can monkeypatch
    `time.monotonic` via the `dedup.time.monotonic` attribute path.
    """
    state_path = path or default_state_path()
    now = clock() if clock is not None else time.monotonic()
    with _locked(state_path) as fd:
        data = _read(fd)
        data = {k: ts for k, ts in data.items() if now - ts < ttl}
        seen = key in data
        if not seen:
            data[key] = now
        _write(fd, data)
    return seen


def dedup_key(agent: str, kind: str, session_id: str | None, tool_name: str | None) -> str:
    return f"{agent}:{kind}:{session_id or ''}:{tool_name or ''}"


def forget(key: str, *, path: Path | None = None) -> bool:
    """Atomically drop `key` from the dedup state. Returns True if the key
    existed and was removed. Used by control-plane signals (e.g. Claude Code's
    UserPromptSubmit) to reset per-session suppression markers ahead of the
    fallback TTL."""
    state_path = path or default_state_path()
    with _locked(state_path) as fd:
        data = _read(fd)
        existed = key in data
        if existed:
            del data[key]
            _write(fd, data)
    return existed


def forget_session(agent: str, session_id: str | None, *, path: Path | None = None) -> int:
    """Drop every dedup key involving (agent, session_id). Returns count.

    Called on UserPromptSubmit: a new turn is starting, so all prior markers
    (twin-fire dedup AND cross-kind coalesce) for this session should reset.
    Matches any key that has both `agent` and `session_id` as `:`-delimited
    tokens — covers both `{agent}:{kind}:{sid}:{tool}` and
    `turn_or_idle:{agent}:{sid}` shapes without hard-coding either.
    """
    sid = session_id or ""
    state_path = path or default_state_path()
    with _locked(state_path) as fd:
        data = _read(fd)
        to_drop = [
            k for k in data
            if agent in k.split(":") and sid in k.split(":")
        ]
        for k in to_drop:
            del data[k]
        if to_drop:
            _write(fd, data)
    return len(to_drop)
