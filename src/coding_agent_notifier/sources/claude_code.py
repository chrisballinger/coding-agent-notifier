"""Parse Claude Code hook stdin payloads into normalised `Event`s.

Claude Code invokes its hook commands with a JSON object on stdin that
always carries `hook_event_name`, `cwd`, and `session_id`. The interesting
shapes for our purposes:

  * `{"hook_event_name": "Notification", "notification_type":
     "permission_prompt"|"idle_prompt"|"elicitation_dialog", "message": …}`
  * `{"hook_event_name": "PermissionRequest", "tool_name": …,
     "tool_input": {...}, "permission_suggestions": [...]}` (handled
     specially by `cli.cmd_permissionrequest`; this parser produces the
     non-blocking notification fallback)
  * `{"hook_event_name": "Stop", …}` — the turn just ended
  * `{"hook_event_name": "UserPromptSubmit", …}` — a new user message
     started a new turn (consumed in `cli.cmd_hook` for dedup reset, not
     parsed here)

`parse()` returns `Event | None`; `None` means "this is a known hook
event we intentionally don't route" (e.g. `Notification` with an
unrecognised `notification_type`). The caller treats `None` as a silent
no-op — we never raise from a hook.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..event import Event, EventKind, truncate

# Claude Code hook event names (as they appear in `hook_event_name`)
_NOTIFICATION = "Notification"
_PERMISSION_REQUEST = "PermissionRequest"
_STOP = "Stop"

# Notification.notification_type → EventKind
_NOTIFICATION_TYPE_MAP: dict[str, EventKind] = {
    "permission_prompt": "permission",
    "idle_prompt": "idle_prompt",
    "elicitation_dialog": "elicitation",
}


def parse(payload: dict[str, Any], *, source_app: str | None = None) -> Event | None:
    """Translate a Claude Code hook stdin payload into an Event, or None to skip."""
    hook = payload.get("hook_event_name")
    cwd = Path(payload.get("cwd") or ".")
    session_id = payload.get("session_id")
    raw_transcript = payload.get("transcript_path")
    transcript_path = Path(raw_transcript) if isinstance(raw_transcript, str) and raw_transcript else None

    if hook == _NOTIFICATION:
        ntype = payload.get("notification_type")
        kind = _NOTIFICATION_TYPE_MAP.get(ntype or "")
        if kind is None:
            return None
        msg = payload.get("message") or payload.get("title") or ""
        return Event(
            agent="claude-code",
            kind=kind,
            message=truncate(str(msg)) if msg else "",
            cwd=cwd,
            session_id=session_id,
            source_app=source_app,
            transcript_path=transcript_path,
        )

    if hook == _PERMISSION_REQUEST:
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input") or None
        return Event(
            agent="claude-code",
            kind="permission",
            # Leave message empty — the Slack/Discord layouts already surface the
            # tool name as a structured field, and `tool_input` is rendered by
            # the sink via tool_formatters. Setting `"Tool: X"` here duplicates.
            message="",
            cwd=cwd,
            session_id=session_id,
            tool_name=tool_name,
            tool_input=tool_input if isinstance(tool_input, dict) else None,
            source_app=source_app,
            transcript_path=transcript_path,
        )

    if hook == _STOP:
        return Event(
            agent="claude-code",
            kind="turn_complete",
            message="",
            cwd=cwd,
            session_id=session_id,
            source_app=source_app,
            transcript_path=transcript_path,
        )

    return None
