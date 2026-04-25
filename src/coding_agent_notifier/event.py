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
    "permission": ":pray:",
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
    # PermissionRequest's `permission_suggestions` payload field — the
    # exact rule edits Claude Code would offer the user (e.g. "Approve &
    # add Bash(curl:*) to localSettings"). Stored as a tuple of dicts so
    # the dataclass stays frozen-hashable. None for non-PermissionRequest
    # events or when the harness sent no suggestions.
    permission_suggestions: tuple[dict[str, Any], ...] | None = None

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


def chunk_text(text: str, *, chunk_size: int, max_chars: int = 0) -> list[str]:
    """Split `text` into chunks of <= `chunk_size` chars.

    `chunk_size` is the platform hard limit (Slack section block: 3000,
    Discord embed description: 4096). `max_chars` is the user-set total-length
    cap from config — 0 means "no user cap, only the platform limit applies".
    The cap is inclusive of the trailing "…", matching `truncate()`, so e.g.
    `max_chars=200` produces output whose chunks sum to at most 200 chars.

    Returns `[]` for empty/whitespace-only input.
    """
    text = text.strip()
    if not text:
        return []
    truncated = max_chars > 0 and len(text) > max_chars
    keep = text[: max_chars - 1] if truncated else text
    chunks = [keep[i : i + chunk_size] for i in range(0, len(keep), chunk_size)]
    if truncated:
        last = chunks[-1]
        if len(last) >= chunk_size:
            chunks[-1] = last[:-1] + "…"
        else:
            chunks[-1] = last + "…"
    return chunks
