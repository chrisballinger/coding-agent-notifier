from __future__ import annotations

import json
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
        )

    if hook == _PERMISSION_REQUEST:
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input") or {}
        preview = _tool_input_preview(tool_input)
        return Event(
            agent="claude-code",
            kind="permission",
            message=f"Tool: {tool_name}" if tool_name else "Permission requested",
            cwd=cwd,
            session_id=session_id,
            tool_name=tool_name,
            tool_input_preview=preview,
            source_app=source_app,
        )

    if hook == _STOP:
        return Event(
            agent="claude-code",
            kind="turn_complete",
            message="Turn complete",
            cwd=cwd,
            session_id=session_id,
            source_app=source_app,
        )

    return None


def _tool_input_preview(tool_input: dict[str, Any]) -> str | None:
    if not tool_input:
        return None
    # Bash-like tools carry the command directly.
    if isinstance(tool_input.get("command"), str):
        return truncate(tool_input["command"])
    if isinstance(tool_input.get("description"), str):
        return truncate(tool_input["description"])
    # Fall back to compact JSON for other tools.
    try:
        return truncate(json.dumps(tool_input, ensure_ascii=False))
    except (TypeError, ValueError):
        return None
