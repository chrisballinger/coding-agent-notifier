"""Parse Codex hook stdin payloads into normalised `Event`s.

Codex sends two distinct payload shapes that we need to handle:

  1. The legacy `notify` program (`~/.codex/config.toml` has
     `notify = ["agent-notify", "hook", "--source", "codex"]`). The
     payload is a small JSON object:
       `{"type": "agent-turn-complete",
         "last-assistant-message": "…",
         "turn-id": "…"}`

  2. The newer `~/.codex/hooks.json` system, which mirrors Claude Code's
     hook schema:
       `{"hook_event_name": "Stop"|"PermissionRequest", "cwd": …,
         "session_id": …, "tool_name": …, "tool_input": {...}}`

`parse()` returns `Event | None`; `None` means we intentionally don't
route this payload (unknown event type). Adding a new event kind: extend
the dispatch in this function and add a fixture under
`tests/fixtures/codex_*.json`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..event import Event


def parse(payload: dict[str, Any], *, source_app: str | None = None) -> Event | None:
    cwd = Path(payload.get("cwd") or ".")

    # Hooks.json shape
    hook = payload.get("hook_event_name")
    if hook == "Stop":
        return Event(
            agent="codex",
            kind="turn_complete",
            message=str(payload.get("message") or "").strip(),
            cwd=cwd,
            session_id=payload.get("session_id"),
            source_app=source_app,
        )
    if hook == "PermissionRequest":
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input") or None
        return Event(
            agent="codex",
            kind="permission",
            message="",
            cwd=cwd,
            session_id=payload.get("session_id"),
            tool_name=tool_name,
            tool_input=tool_input if isinstance(tool_input, dict) else None,
            source_app=source_app,
        )

    # Legacy notify shape
    ntype = payload.get("type")
    if ntype == "agent-turn-complete":
        msg = payload.get("last-assistant-message") or ""
        return Event(
            agent="codex",
            kind="turn_complete",
            message=str(msg).strip() if msg else "",
            cwd=cwd,
            session_id=payload.get("turn-id") or payload.get("session_id"),
            source_app=source_app,
        )

    return None
