from __future__ import annotations

from dataclasses import dataclass

from ..config import DiscordConfig, Verbosity
from ..event import Event, EventKind, chunk_text
from ..tool_formatters import DEFAULT_MAX_CHARS, render
from .base import SinkError, http_post_json

# Discord embed `description` hard cap is 4096 chars (leave headroom for joins).
_EMBED_DESC_CHUNK_SIZE = 4000
# Discord allows up to 10 embeds per message.
_EMBED_MAX_PER_MESSAGE = 10

# Discord embed colors are decimal ints, not hex strings.
_KIND_COLORS: dict[EventKind, int] = {
    "permission": 0xE01E5A,
    "idle_prompt": 0xECB22E,
    "elicitation": 0xECB22E,
    "turn_complete": 0x2EB67D,
}
_DANGER_COLOR = 0xA30F18


@dataclass
class DiscordSink:
    config: DiscordConfig
    name: str = "discord"
    tool_input_max_chars: int = DEFAULT_MAX_CHARS
    verbosity: Verbosity = "normal"
    message_max_chars: int = 0

    def send(self, event: Event) -> None:
        if not self.config.enabled:
            return
        if not self.config.webhook_url:
            raise SinkError("Discord sink enabled but webhook_url is not set")
        body = build_discord_message(
            event,
            max_chars=self.tool_input_max_chars,
            verbosity=self.verbosity,
            message_max_chars=self.message_max_chars,
        )
        status, text = http_post_json(self.config.webhook_url, body)
        if status >= 300:
            raise SinkError(f"Discord webhook returned {status}: {text!r}")


def build_discord_message(
    event: Event,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    verbosity: Verbosity = "normal",
    message_max_chars: int = 0,
) -> dict:
    if verbosity == "minimal":
        # Payload-free ping: no tool render, no danger emoji (would itself
        # leak that a Bash command looked risky), no color hint, no footer.
        return {
            "embeds": [{
                "title": event.title,
                "color": 0x95A5A6,
            }]
        }

    tool = render(event.tool_name, event.tool_input, max_chars=max_chars)
    color = _DANGER_COLOR if tool.dangerous else _KIND_COLORS.get(event.kind, 0x95A5A6)

    danger_prefix = "🚨 " if tool.dangerous else ""
    title = f"{danger_prefix}{event.title}"

    summary = tool.summary
    if verbosity == "terse" and event.tool_name and summary:
        summary = f"**{event.tool_name}:** {summary}"
    elif verbosity == "terse" and event.tool_name and not event.message and not summary:
        summary = f"**{event.tool_name}**"

    tail_parts: list[str] = []
    if summary:
        tail_parts.append(summary)
    if tool.detail:
        if tool.code_block:
            lang = tool.code_block_lang
            fence = f"```{lang}\n" if lang else "```\n"
            tail_parts.append(f"{fence}{tool.detail}\n```")
        else:
            tail_parts.append(tool.detail)
    tail_text = "\n\n".join(tail_parts)

    descriptions = _build_descriptions(event.message, tail_text, message_max_chars)

    first_embed: dict = {
        "title": title,
        "description": descriptions[0] if descriptions else "",
        "color": color,
    }

    if verbosity == "normal":
        fields: list[dict] = [
            {"name": "Project", "value": event.cwd.name or str(event.cwd), "inline": True},
        ]
        if event.session_id:
            fields.append({"name": "Session", "value": event.session_id[:8], "inline": True})
        if event.tool_name:
            fields.append({"name": "Tool", "value": event.tool_name, "inline": True})
        if event.source_app:
            fields.append({"name": "App", "value": event.source_app, "inline": True})
        first_embed["fields"] = fields
    else:
        footer = _terse_footer(event)
        if footer:
            first_embed["footer"] = {"text": footer}

    embeds: list[dict] = [first_embed]
    for cont in descriptions[1:]:
        embeds.append({"description": cont, "color": color})
    return {"embeds": embeds}


def _build_descriptions(message: str, tail_text: str, message_max_chars: int) -> list[str]:
    """Compose embed descriptions: chunked event.message followed by the tail.

    The tail (tool summary + tool detail) joins onto the last message chunk if
    it fits, else into a new continuation embed. Result is capped at 10
    embeds; pathological overflow gets a trailing "…" on the last chunk.
    """
    body_chunks = chunk_text(
        message, chunk_size=_EMBED_DESC_CHUNK_SIZE, max_chars=message_max_chars,
    ) if message else []

    descriptions: list[str] = list(body_chunks)
    if tail_text:
        if descriptions:
            joined = descriptions[-1] + "\n\n" + tail_text
            if len(joined) <= _EMBED_DESC_CHUNK_SIZE:
                descriptions[-1] = joined
            else:
                descriptions.append(tail_text[:_EMBED_DESC_CHUNK_SIZE])
        else:
            descriptions.append(tail_text[:_EMBED_DESC_CHUNK_SIZE])

    if len(descriptions) > _EMBED_MAX_PER_MESSAGE:
        descriptions = descriptions[:_EMBED_MAX_PER_MESSAGE]
        last = descriptions[-1]
        descriptions[-1] = last[: _EMBED_DESC_CHUNK_SIZE - 1].rstrip() + "…"
    return descriptions


def _terse_footer(event: Event) -> str:
    bits: list[str] = []
    cwd_name = event.cwd.name or str(event.cwd)
    if cwd_name:
        bits.append(cwd_name)
    if event.session_id:
        bits.append(event.session_id[:8])
    if event.source_app:
        bits.append(event.source_app)
    return " · ".join(bits)
