from __future__ import annotations

import json
from dataclasses import dataclass

from ..config import SlackConfig
from ..event import Event
from .base import SinkError, http_post_json

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"


@dataclass
class SlackSink:
    config: SlackConfig
    name: str = "slack"

    def send(self, event: Event) -> None:
        if not self.config.enabled:
            return
        body = build_slack_message(event)
        if self.config.webhook_url:
            status, text = http_post_json(self.config.webhook_url, body)
            if status >= 300 or text.strip() not in ("ok", ""):
                raise SinkError(f"Slack webhook returned {status}: {text!r}")
            return
        if self.config.bot_token:
            channel = self.config.channel or "@me"
            if channel == "@me":
                channel = resolve_self_channel(self.config.bot_token)
            payload = {"channel": channel, **body}
            status, text = http_post_json(
                SLACK_POST_MESSAGE_URL,
                payload,
                headers={"Authorization": f"Bearer {self.config.bot_token}"},
            )
            if status >= 300:
                raise SinkError(f"Slack API {status}: {text!r}")
            parsed = _safe_json(text)
            if not parsed.get("ok"):
                raise SinkError(f"Slack API error: {parsed.get('error', text)!r}")
            return
        raise SinkError("Slack sink has neither webhook_url nor bot_token configured")


def resolve_self_channel(bot_token: str) -> str:
    """Call auth.test to find the bot's own user id, so we can DM 'self'."""
    status, text = http_post_json(
        SLACK_AUTH_TEST_URL,
        {},
        headers={"Authorization": f"Bearer {bot_token}"},
    )
    if status >= 300:
        raise SinkError(f"Slack auth.test HTTP {status}: {text!r}")
    parsed = _safe_json(text)
    if not parsed.get("ok") or not parsed.get("user_id"):
        raise SinkError(f"Slack auth.test error: {parsed.get('error', text)!r}")
    return parsed["user_id"]


def build_slack_message(event: Event) -> dict:
    cwd_name = event.cwd.name or str(event.cwd)
    session_short = (event.session_id or "")[:8] or "—"
    fields = [
        {"type": "mrkdwn", "text": f"*Project:*\n`{cwd_name}`"},
        {"type": "mrkdwn", "text": f"*Session:*\n`{session_short}`"},
    ]
    if event.tool_name:
        fields.append({"type": "mrkdwn", "text": f"*Tool:*\n`{event.tool_name}`"})
    if event.source_app:
        fields.append({"type": "mrkdwn", "text": f"*App:*\n{event.source_app}"})

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{event.emoji} {event.title}", "emoji": True},
        },
        {"type": "section", "fields": fields},
    ]
    if event.message:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": event.message}}
        )
    if event.tool_input_preview:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"```\n{event.tool_input_preview}\n```",
                },
            }
        )

    return {"text": f"{event.title} — {event.message}".strip(" —"), "blocks": blocks}


def _safe_json(text: str) -> dict:
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
