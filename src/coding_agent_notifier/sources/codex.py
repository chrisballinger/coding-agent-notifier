from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..event import Event, truncate

# Codex stdin can arrive in two shapes:
#   1. The `notify` program (config.toml `notify = [...]`): a small JSON payload
#      with a `type` like "agent-turn-complete" and a `last-assistant-message`.
#   2. The newer `hooks.json` system: the same schema as Claude Code's hooks
#      (hook_event_name, session_id, cwd, …).


def parse(payload: dict[str, Any], *, source_app: str | None = None) -> Event | None:
    cwd = Path(payload.get("cwd") or ".")

    # Hooks.json shape
    hook = payload.get("hook_event_name")
    if hook == "Stop":
        return Event(
            agent="codex",
            kind="turn_complete",
            message=truncate(str(payload.get("message") or "Turn complete")),
            cwd=cwd,
            session_id=payload.get("session_id"),
            source_app=source_app,
        )
    if hook == "PermissionRequest":
        tool_name = payload.get("tool_name")
        preview = _tool_input_preview(payload.get("tool_input") or {})
        return Event(
            agent="codex",
            kind="permission",
            message=f"Tool: {tool_name}" if tool_name else "Permission requested",
            cwd=cwd,
            session_id=payload.get("session_id"),
            tool_name=tool_name,
            tool_input_preview=preview,
            source_app=source_app,
        )

    # Legacy notify shape
    ntype = payload.get("type")
    if ntype == "agent-turn-complete":
        msg = payload.get("last-assistant-message") or "Turn complete"
        return Event(
            agent="codex",
            kind="turn_complete",
            message=truncate(str(msg)),
            cwd=cwd,
            session_id=payload.get("turn-id") or payload.get("session_id"),
            source_app=source_app,
        )

    return None


def _tool_input_preview(tool_input: dict[str, Any]) -> str | None:
    if not tool_input:
        return None
    if isinstance(tool_input.get("command"), str):
        return truncate(tool_input["command"])
    try:
        return truncate(json.dumps(tool_input, ensure_ascii=False))
    except (TypeError, ValueError):
        return None
