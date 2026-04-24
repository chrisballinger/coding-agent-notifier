from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..config import SlackConfig, Verbosity
from ..event import Event, EventKind
from ..tool_formatters import DEFAULT_MAX_CHARS, ToolRender, render
from .base import SinkError, http_post_json

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"

# Slack attachment color bar per event kind (hex without #).
_KIND_COLORS: dict[EventKind, str] = {
    "permission": "#e01e5a",
    "idle_prompt": "#ecb22e",
    "elicitation": "#ecb22e",
    "turn_complete": "#2eb67d",
}
_DANGER_COLOR = "#a30f18"


@dataclass
class SlackSink:
    config: SlackConfig
    name: str = "slack"
    tool_input_max_chars: int = DEFAULT_MAX_CHARS
    verbosity: Verbosity = "normal"

    def send(self, event: Event) -> None:
        if not self.config.enabled:
            return
        body = build_slack_message(
            event,
            max_chars=self.tool_input_max_chars,
            verbosity=self.verbosity,
        )
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


def build_slack_message(
    event: Event,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    verbosity: Verbosity = "normal",
) -> dict:
    tool = render(event.tool_name, event.tool_input, max_chars=max_chars)
    dangerous = tool.dangerous

    header = f"{':rotating_light: ' if dangerous else ''}{event.emoji} {event.title}"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}},
    ]

    if verbosity == "normal":
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
        blocks.append({"type": "section", "fields": fields})

    body = _compose_body(event, tool, verbosity)
    if body:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
    if tool.detail:
        detail_text = f"```\n{tool.detail}\n```" if tool.code_block else tool.detail
        blocks.append(
            {"type": "section",
             "text": {"type": "mrkdwn", "text": detail_text}}
        )

    if verbosity == "terse":
        footer = _terse_footer(event)
        if footer:
            blocks.append(
                {"type": "context",
                 "elements": [{"type": "mrkdwn", "text": footer}]}
            )

    attachment = {
        "color": _DANGER_COLOR if dangerous else _KIND_COLORS.get(event.kind, ""),
        "blocks": blocks,
    }
    return {
        "text": _fallback_text(event, tool, dangerous),
        "attachments": [attachment],
    }


def _compose_body(event: Event, tool: ToolRender, verbosity: Verbosity) -> str:
    parts = []
    if event.message:
        parts.append(_mrkdwn_polish(event.message))
    summary = tool.summary
    # In terse mode the field block is gone, so fold the tool name into the body
    # (`*Bash:* echo hi`) instead of relying on a separate Tool: field.
    if verbosity == "terse" and event.tool_name and summary:
        summary = f"*{event.tool_name}:* {summary}"
    elif verbosity == "terse" and event.tool_name and not event.message and not summary:
        summary = f"*{event.tool_name}*"
    if summary:
        parts.append(summary)
    return "\n".join(parts)


def _terse_footer(event: Event) -> str:
    bits: list[str] = []
    cwd_name = event.cwd.name or str(event.cwd)
    if cwd_name:
        bits.append(f"`{cwd_name}`")
    if event.session_id:
        bits.append(f"`{event.session_id[:8]}`")
    if event.source_app:
        bits.append(event.source_app)
    return " · ".join(bits)


# Path regex excludes URL-like contexts: `/` preceded by `:`, letter, digit, or
# backtick shouldn't become inline code. So `https://...`, `a/b` (path fragments
# in identifiers), and already-quoted paths all pass through untouched.
_PATH_RE = re.compile(r"(?<![`:/A-Za-z0-9])(/[^\s`<>]+)")
_URL_RE = re.compile(r"(?<![<`])\bhttps?://\S+")


def _mrkdwn_polish(text: str) -> str:
    """Lightly format paths as inline code and URLs as Slack links."""
    def _link(m: re.Match[str]) -> str:
        url = m.group(0).rstrip(".,)")
        host = re.sub(r"^https?://", "", url).split("/", 1)[0]
        return f"<{url}|{host}>"
    text = _URL_RE.sub(_link, text)
    text = _PATH_RE.sub(lambda m: f"`{m.group(1).rstrip('.,)')}`", text)
    return text


def _fallback_text(event: Event, tool: ToolRender, dangerous: bool) -> str:
    bits = []
    if dangerous:
        bits.append("⚠️ DANGEROUS")
    bits.append(event.title)
    summary = tool.summary or event.message
    if summary:
        bits.append(summary)
    return " — ".join(b for b in bits if b)


def _safe_json(text: str) -> dict:
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
