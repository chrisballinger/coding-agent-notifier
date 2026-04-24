from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

EventKind = Literal["permission", "idle_prompt", "turn_complete", "elicitation"]
AgentName = Literal["claude-code", "codex"]

KIND_TITLES: dict[EventKind, str] = {
    "permission": "needs approval",
    "idle_prompt": "is waiting on you",
    "turn_complete": "finished a turn",
    "elicitation": "needs input (MCP)",
}

KIND_EMOJI: dict[EventKind, str] = {
    "permission": ":warning:",
    "idle_prompt": ":hourglass_flowing_sand:",
    "turn_complete": ":white_check_mark:",
    "elicitation": ":incoming_envelope:",
}

AGENT_LABELS: dict[AgentName, str] = {
    "claude-code": "Claude Code",
    "codex": "Codex",
}


@dataclass(frozen=True)
class Event:
    agent: AgentName
    kind: EventKind
    message: str
    cwd: Path
    session_id: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    source_app: str | None = None
    transcript_path: Path | None = None

    @property
    def title(self) -> str:
        return f"{AGENT_LABELS[self.agent]} {KIND_TITLES[self.kind]}"

    @property
    def emoji(self) -> str:
        return KIND_EMOJI[self.kind]


def truncate(text: str, limit: int = 200) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
