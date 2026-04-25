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
SLACK_CHAT_UPDATE_URL = "https://slack.com/api/chat.update"

APPROVE_ACTION_ID = "agent_notify_approve"
DENY_ACTION_ID = "agent_notify_deny"
# Per-option AskUserQuestion buttons. Suffix is the index into
# tool_input["questions"][0]["options"]. Daemon parses the index out of
# action_id and stores it on the resolved approval record.
OPTION_ACTION_ID_PREFIX = "agent_notify_option_"
# Slack button text limit; truncate option labels to fit.
_BUTTON_TEXT_MAX = 75

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
                channel = _dm_target(self.config)
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


def resolve_self_channel(bot_token: str, *, poster: "callable | None" = None) -> str:
    """Call auth.test to find the bot's own user id, so we can DM 'self'."""
    _post = poster or http_post_json
    status, text = _post(
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


def _dm_target(slack_config: SlackConfig, *, poster: "callable | None" = None) -> str:
    """Resolve `channel = "@me"` to a user_id for chat.postMessage.

    Prefer ``approver_user_ids[0]`` — the installing user, populated by the
    wizard. Posting to a user_id opens a 1:1 DM with that user (Slack
    auto-creates the IM channel) so the message arrives as a normal DM
    ping.

    Fall back to the bot's own user_id via ``auth.test`` only when no
    approvers are configured. That lands in the bot's App Home Messages
    tab (visible only with the ``messages_tab_enabled`` manifest flag) —
    a degraded mode for users who deliberately use the
    DM-only-no-allowlist setup.
    """
    if slack_config.approver_user_ids:
        return slack_config.approver_user_ids[0]
    return resolve_self_channel(slack_config.bot_token, poster=poster)


def build_slack_message(
    event: Event,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    verbosity: Verbosity = "normal",
) -> dict:
    # In minimal mode we never touch `tool_input` or the tool summary, so skip
    # the render call entirely — even an in-memory DANGEROUS_BASH_PATTERNS
    # match is data we don't need to compute. The return value has a single
    # header block, a generic fallback, no attachment color hint (avoids
    # signaling danger, which itself could leak that a Bash command looked
    # risky). Buttons are added separately by `build_approval_message`.
    if verbosity == "minimal":
        return _build_minimal_message(event)

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
        if tool.code_block:
            lang = tool.code_block_lang
            fence = f"```{lang}\n" if lang else "```\n"
            detail_text = f"{fence}{tool.detail}\n```"
        else:
            detail_text = tool.detail
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
        "text": _fallback_text(event, tool, dangerous, verbosity=verbosity),
        "attachments": [attachment],
    }


def _build_minimal_message(event: Event) -> dict:
    """Payload-free message for compliance-sensitive environments.

    Contains ONLY `event.title` (e.g. "Claude Code needs approval"). No
    tool name, no tool_input, no message body, no transcript snippet, no
    cwd, no session id, no source app, no color bar. The user gets a
    ping; the terminal is authoritative for what's actually pending.
    """
    title = event.title  # "Claude Code needs approval" etc — no cwd / tool / etc
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{event.emoji} {title}", "emoji": True}},
    ]
    return {
        "text": title,
        "attachments": [{"blocks": blocks}],
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


_IOS_PREVIEW_MAX = 140


def _fallback_text(event: Event, tool: ToolRender, dangerous: bool, *,
                   verbosity: Verbosity = "normal") -> str:
    """Compose the Slack `text` field — what iOS renders in the push preview.

    Slack mobile also displays this above the attachment in-app, so a full-body
    dump produces visible duplication. Keep it to a single line, capped at
    140 chars; the rich content still lives in the attachment blocks. In
    `minimal` verbosity the preview is just the event title — no command,
    no code.
    """
    if verbosity == "minimal":
        return event.title
    bits = []
    if dangerous:
        bits.append("⚠️ DANGEROUS")
    bits.append(event.title)
    summary = tool.summary or event.message
    if summary:
        bits.append(_single_line(summary, _IOS_PREVIEW_MAX))
    return " — ".join(b for b in bits if b)


def _single_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())  # collapse whitespace + newlines
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _safe_json(text: str) -> dict:
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ask_user_question_options(
    tool_name: str | None,
    tool_input: dict | None,
) -> list[str] | None:
    """Return the option labels of an AskUserQuestion's first question, or None.

    Returns None for any non-AskUserQuestion tool, for malformed payloads,
    or for `multiSelect: true` (out of scope for v1 — single-select only).
    """
    if tool_name != "AskUserQuestion" or not isinstance(tool_input, dict):
        return None
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    q = questions[0]
    if not isinstance(q, dict):
        return None
    if q.get("multiSelect") is True:
        return None
    options = q.get("options")
    if not isinstance(options, list) or not options:
        return None
    labels: list[str] = []
    for opt in options:
        if not isinstance(opt, dict):
            return None
        label = opt.get("label")
        if not isinstance(label, str) or not label:
            return None
        labels.append(label)
    return labels


def _selected_label_from_record(rec: dict) -> str | None:
    """Look up the selected option's label from a resolved approval record."""
    idx = rec.get("selected_option_index")
    if not isinstance(idx, int):
        return None
    labels = _ask_user_question_options(rec.get("tool_name"), rec.get("tool_input"))
    if labels is None or not (0 <= idx < len(labels)):
        return None
    return labels[idx]


def _truncate_button_text(text: str) -> str:
    if len(text) <= _BUTTON_TEXT_MAX:
        return text
    return text[: _BUTTON_TEXT_MAX - 1] + "…"  # …


def _build_option_buttons(approval_id: str, labels: list[str]) -> dict:
    """Block Kit actions block: one button per option label + a Deny button."""
    elements: list[dict] = []
    for i, label in enumerate(labels):
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": _truncate_button_text(label), "emoji": False},
            "action_id": f"{OPTION_ACTION_ID_PREFIX}{i}",
            "value": approval_id,
        })
    elements.append({
        "type": "button",
        "text": {"type": "plain_text", "text": "Deny", "emoji": False},
        "style": "danger",
        "action_id": DENY_ACTION_ID,
        "value": approval_id,
    })
    # Slack caps an actions block at 25 elements. With 1 Deny that means
    # 24 options. Anything beyond that should fall back to a static_select
    # — punted out of scope; just truncate to fit and log it.
    if len(elements) > 25:
        elements = elements[:24] + [elements[-1]]
    return {
        "type": "actions",
        "block_id": f"agent_notify::{approval_id}",
        "elements": elements,
    }


def _build_approve_deny_block(approval_id: str, confirm_text: str) -> dict:
    return {
        "type": "actions",
        "block_id": f"agent_notify::{approval_id}",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Approve", "emoji": False},
                "style": "primary",
                "action_id": APPROVE_ACTION_ID,
                "value": approval_id,
                "confirm": {
                    "title": {"type": "plain_text", "text": "Approve tool call?"},
                    "text": {"type": "mrkdwn", "text": confirm_text},
                    "confirm": {"type": "plain_text", "text": "Approve"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                },
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Deny", "emoji": False},
                "style": "danger",
                "action_id": DENY_ACTION_ID,
                "value": approval_id,
            },
        ],
    }


def build_approval_message(
    event: Event,
    approval_id: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    verbosity: Verbosity = "terse",
) -> dict:
    """Like `build_slack_message` but with an actions block for the user.

    For most tools, two buttons: Approve (primary, with confirm dialog so an
    accidental lock-screen tap doesn't run a Bash command) and Deny (danger,
    no confirm — fail-safe direction).

    For AskUserQuestion, one button per option label + a Deny button. Tapping
    an option resolves the approval AND threads the selected index back to
    the hook so it pre-fills the answer via PermissionRequest's
    `updatedInput`. Falls back to Approve/Deny if the AskUserQuestion payload
    is malformed or uses multiSelect (v1 single-select only).
    """
    body = build_slack_message(event, max_chars=max_chars, verbosity=verbosity)

    option_labels = _ask_user_question_options(event.tool_name, event.tool_input)
    if option_labels:
        actions_block = _build_option_buttons(approval_id, option_labels)
    else:
        # The confirm dialog is part of the Slack payload — in minimal mode
        # it would itself leak tool_name / cwd. Strip it to a generic prompt
        # so the buttons stay opaque to anyone reading the message.
        if verbosity == "minimal":
            confirm_text = "Approve pending tool call?"
        else:
            tool_label = event.tool_name or "tool"
            confirm_text = (
                f"Allow `{tool_label}` to run in "
                f"`{event.cwd.name or str(event.cwd)}`?"
            )
        actions_block = _build_approve_deny_block(approval_id, confirm_text)

    # Append to the first attachment so the color bar / fallback text stay
    # intact. Falls back to top-level blocks if the builder ever stops using
    # attachments (defensive).
    if body.get("attachments"):
        body["attachments"][0].setdefault("blocks", []).append(actions_block)
    else:
        body.setdefault("blocks", []).append(actions_block)
    return body


def post_approval_message(
    event: Event,
    slack_config: SlackConfig,
    approval_id: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    verbosity: Verbosity = "terse",
    poster: "callable | None" = None,
) -> tuple[str, str]:
    """Post an approval message, return (channel_id, message_ts).

    `poster` is injectable for tests (matches existing `_FakePoster` pattern in
    test_sinks.py). Production passes the default `http_post_json`.
    """
    if not slack_config.bot_token:
        raise SinkError("approval messages require bot_token (webhook cannot carry interactivity)")
    body = build_approval_message(event, approval_id, max_chars=max_chars, verbosity=verbosity)
    channel = slack_config.channel or "@me"
    if channel == "@me":
        channel = _dm_target(slack_config, poster=poster)
    payload = {"channel": channel, **body}
    _post = poster or http_post_json
    status, text = _post(
        SLACK_POST_MESSAGE_URL,
        payload,
        headers={"Authorization": f"Bearer {slack_config.bot_token}"},
    )
    if status >= 300:
        raise SinkError(f"Slack chat.postMessage {status}: {text!r}")
    parsed = _safe_json(text)
    if not parsed.get("ok"):
        raise SinkError(f"Slack API error: {parsed.get('error', text)!r}")
    ts = parsed.get("ts")
    posted_channel = parsed.get("channel") or channel
    if not isinstance(ts, str) or not ts:
        raise SinkError(f"Slack did not return a message ts: {text!r}")
    return posted_channel, ts


def build_resolved_message(
    event: Event,
    decision: str,
    actor_label: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    verbosity: Verbosity = "terse",
    selected_label: str | None = None,
) -> dict:
    """Block Kit replacement for `chat.update` after approve/deny or timeout.

    Preserves the tool summary so scroll-back context isn't lost, but strips
    the buttons and swaps the header to the outcome.

    When `selected_label` is provided (AskUserQuestion option click), the
    header reads "Selected `<label>` by @user" instead of the generic
    "Approved by @user". Decision is still "allow" — the selection both
    approves the tool call AND chooses the answer.
    """
    if decision == "allow":
        icon = ":white_check_mark:"
        verb = "Approved"
        color = "#2eb67d"
        if selected_label:
            verb = f"Selected `{selected_label}`"
    elif decision == "deny":
        icon = ":no_entry_sign:"
        verb = "Denied"
        color = "#e01e5a"
    else:  # "timeout"
        icon = ":hourglass:"
        verb = "Timed out — denied"
        color = "#a0a0a0"

    header = f"{icon} *{verb} by {actor_label}*"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
    ]

    if verbosity == "minimal":
        # Outcome + actor only — no tool name, no context footer.
        return {
            "text": f"{verb} by {actor_label}",
            "attachments": [{"color": color, "blocks": blocks}],
        }

    tool = render(event.tool_name, event.tool_input, max_chars=max_chars)
    summary = tool.summary
    if verbosity == "terse" and event.tool_name and summary:
        summary = f"*{event.tool_name}:* {summary}"
    if summary:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": summary}})
    footer = _terse_footer(event) if verbosity == "terse" else None
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return {
        "text": f"{verb} by {actor_label} — {event.tool_name or 'tool'}",
        "attachments": [{"color": color, "blocks": blocks}],
    }


def update_message(
    bot_token: str,
    channel: str,
    ts: str,
    body: dict,
    *,
    poster: "callable | None" = None,
) -> None:
    """Edit a previously-posted message in place via chat.update."""
    _post = poster or http_post_json
    payload = {"channel": channel, "ts": ts, **body}
    status, text = _post(
        SLACK_CHAT_UPDATE_URL,
        payload,
        headers={"Authorization": f"Bearer {bot_token}"},
    )
    if status >= 300:
        raise SinkError(f"Slack chat.update {status}: {text!r}")
    parsed = _safe_json(text)
    if not parsed.get("ok"):
        raise SinkError(f"Slack chat.update error: {parsed.get('error', text)!r}")
