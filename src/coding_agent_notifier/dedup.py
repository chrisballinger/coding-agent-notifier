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
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Return True iff `key` was recorded within the last `ttl` seconds.

    Otherwise records `key` at the current time and returns False. Expired keys
    are garbage-collected on every call so the state file can't grow unbounded.
    """
    state_path = path or default_state_path()
    now = clock()
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
