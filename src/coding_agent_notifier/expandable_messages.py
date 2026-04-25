"""On-disk store for messages that can be expanded/collapsed in place.

When `SlackSink.send` posts a message whose body would be truncated by the
preview budget, it persists both Slack payloads (preview + full) keyed by
a uuid `message_id`. The Slack daemon, on receiving an `agent_notify_expand`
or `agent_notify_collapse` button click, reads the record and `chat.update`s
the original message in place to swap views.

Mirrors the on-disk shape of `pending_approvals` (one JSON file per id,
sanitized filename, optional base_dir for tests, periodic gc_stale). No
FIFO and no lock — the click path is read-only relative to the record, and
writes only happen at create time, so locking would buy us nothing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable


def default_dir() -> Path:
    from . import paths
    return paths.expandable_messages_dir()


def _record_path(message_id: str, base_dir: Path | None) -> Path:
    return (base_dir or default_dir()) / f"{_safe(message_id)}.json"


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)


def create(
    message_id: str,
    *,
    workspace: str,
    channel: str,
    message_ts: str,
    preview_body: dict,
    full_body: dict,
    base_dir: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> Path:
    """Persist the (preview, full) Slack payloads for a posted message.

    Returns the record path. Caller is expected to embed `message_id` in
    the Show more / Show less button's `value` field so the daemon's click
    handler can look this record up.
    """
    record = _record_path(message_id, base_dir)
    record.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "message_id": message_id,
        "workspace": workspace,
        "channel": channel,
        "message_ts": message_ts,
        "preview_body": preview_body,
        "full_body": full_body,
        "created_at": clock(),
    }
    from . import paths as _paths
    _paths.write_secure(record, json.dumps(payload))
    return record


def read(message_id: str, *, base_dir: Path | None = None) -> dict | None:
    record = _record_path(message_id, base_dir)
    if not record.exists():
        return None
    try:
        return json.loads(record.read_text())
    except (OSError, ValueError):
        return None


def cleanup(message_id: str, *, base_dir: Path | None = None) -> None:
    """Remove the record. Best-effort; swallows errors."""
    try:
        _record_path(message_id, base_dir).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def gc_stale(
    *,
    max_age_seconds: float = 7 * 86400.0,
    base_dir: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> int:
    """Remove records older than `max_age_seconds`. Returns count removed.

    Default TTL is 7 days — the message itself stays in Slack indefinitely,
    but after a week the user clicking Show more on a stale notification is
    a low-value path; cleaner to log "unknown_toggle" and leave the message
    as-is than to keep state forever.
    """
    root = base_dir or default_dir()
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
        mid = data.get("message_id")
        if isinstance(mid, str):
            cleanup(mid, base_dir=base_dir)
            removed += 1
    return removed
