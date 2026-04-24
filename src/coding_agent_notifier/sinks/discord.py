from __future__ import annotations

from dataclasses import dataclass

from ..config import DiscordConfig
from ..event import Event, EventKind
from ..tool_formatters import DEFAULT_MAX_CHARS, render
from .base import SinkError, http_post_json

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

    def send(self, event: Event) -> None:
        if not self.config.enabled:
            return
        if not self.config.webhook_url:
            raise SinkError("Discord sink enabled but webhook_url is not set")
        body = build_discord_message(event, max_chars=self.tool_input_max_chars)
        status, text = http_post_json(self.config.webhook_url, body)
        if status >= 300:
            raise SinkError(f"Discord webhook returned {status}: {text!r}")


def build_discord_message(event: Event, *, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    tool = render(event.tool_name, event.tool_input, max_chars=max_chars)
    color = _DANGER_COLOR if tool.dangerous else _KIND_COLORS.get(event.kind, 0x95A5A6)

    danger_prefix = "🚨 " if tool.dangerous else ""
    title = f"{danger_prefix}{event.title}"

    fields: list[dict] = [
        {"name": "Project", "value": event.cwd.name or str(event.cwd), "inline": True},
    ]
    if event.session_id:
        fields.append({"name": "Session", "value": event.session_id[:8], "inline": True})
    if event.tool_name:
        fields.append({"name": "Tool", "value": event.tool_name, "inline": True})
    if event.source_app:
        fields.append({"name": "App", "value": event.source_app, "inline": True})

    description_parts: list[str] = []
    if event.message:
        description_parts.append(event.message)
    if tool.summary:
        description_parts.append(tool.summary)
    if tool.detail:
        description_parts.append(f"```\n{tool.detail}\n```")

    embed: dict = {
        "title": title,
        "description": "\n\n".join(description_parts)[:4000],
        "fields": fields,
        "color": color,
    }
    return {"embeds": [embed]}
