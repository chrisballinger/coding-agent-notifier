from __future__ import annotations

from dataclasses import dataclass

from ..config import DiscordConfig
from ..event import Event
from .base import SinkError, http_post_json


@dataclass
class DiscordSink:
    """Scaffold only — v2. Webhook embed payload shape."""

    config: DiscordConfig
    name: str = "discord"

    def send(self, event: Event) -> None:
        if not self.config.enabled:
            return
        if not self.config.webhook_url:
            raise SinkError("Discord sink enabled but webhook_url is not set")
        body = build_discord_message(event)
        status, text = http_post_json(self.config.webhook_url, body)
        if status >= 300:
            raise SinkError(f"Discord webhook returned {status}: {text!r}")


def build_discord_message(event: Event) -> dict:
    fields = [
        {"name": "Project", "value": event.cwd.name or str(event.cwd), "inline": True},
    ]
    if event.session_id:
        fields.append({"name": "Session", "value": event.session_id[:8], "inline": True})
    if event.tool_name:
        fields.append({"name": "Tool", "value": event.tool_name, "inline": True})

    embed: dict = {
        "title": event.title,
        "description": event.message or "",
        "fields": fields,
    }
    if event.tool_input_preview:
        embed["description"] = (
            (embed["description"] + "\n\n") if embed["description"] else ""
        ) + f"```\n{event.tool_input_preview}\n```"
    return {"embeds": [embed]}
