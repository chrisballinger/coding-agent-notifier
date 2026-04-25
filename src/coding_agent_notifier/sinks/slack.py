from __future__ import annotations

import dataclasses
import json
import uuid
from dataclasses import dataclass

from .. import expandable_messages, transcript
from ..config import SlackConfig, Verbosity
from ..event import AGENT_LABELS, Event, EventKind, chunk_text
from ..tool_formatters import DEFAULT_MAX_CHARS, ToolRender, render
from . import mrkdwn as _mrkdwn
from .base import SinkError, http_post_json

# Slack section-block mrkdwn hard cap is 3000 chars; leave headroom for the
# `_mrkdwn_polish` URL/path wrappers that lengthen text slightly.
_SECTION_CHUNK_SIZE = 2900

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"
SLACK_CHAT_UPDATE_URL = "https://slack.com/api/chat.update"

APPROVE_ACTION_ID = "agent_notify_approve"
DENY_ACTION_ID = "agent_notify_deny"
# Per-option AskUserQuestion buttons. Suffix is the index into
# tool_input["questions"][0]["options"]. Daemon parses the index out of
# action_id and stores it on the resolved approval record.
OPTION_ACTION_ID_PREFIX = "agent_notify_option_"
# Per-suggestion approve buttons rendered alongside Approve/Deny on
# non-AskUserQuestion tools. Suffix is the index into the approval's
# permission_suggestions list. Tapping one resolves the approval as
# allow + emits PermissionRequest's `decision.updatedPermissions`.
SUGGESTION_ACTION_ID_PREFIX = "agent_notify_suggestion_"
# Per-question "Custom answer" buttons on AskUserQuestion. Suffix is the
# question index. Tapping one opens a modal with a text input; the typed
# string becomes that question's answer (overrides any option click).
CUSTOM_ANSWER_ACTION_ID_PREFIX = "agent_notify_custom_answer_"
# "Deny with reason" button (alongside the one-tap Deny). Tapping opens a
# modal with a single text input; the typed string becomes
# `decision.message` on the deny path.
DENY_REASON_ACTION_ID = "agent_notify_deny_reason"
# In-place expand/collapse toggle on long-body messages. The button's `value`
# carries the `message_id` that keys the `expandable_messages` record holding
# the (preview, full) Slack payloads to swap between via chat.update.
EXPAND_ACTION_ID = "agent_notify_expand"
COLLAPSE_ACTION_ID = "agent_notify_collapse"
# Modal callback_ids — daemon's view_submission handler dispatches on these.
MODAL_CALLBACK_CUSTOM_ANSWER = "agent_notify_modal_custom_answer"
MODAL_CALLBACK_DENY_REASON = "agent_notify_modal_deny_reason"
# Block / action IDs used inside the modals to locate the text input value.
MODAL_INPUT_BLOCK_ID = "agent_notify_modal_input"
MODAL_INPUT_ACTION_ID = "agent_notify_modal_input_value"


@dataclass(frozen=True)
class ParsedActionId:
    """Decoded `action_id` from a Slack interactive payload.

    At most one of `decision` / `modal_kind` / `toggle_kind` is set; the
    rest of the fields carry whatever index information the matched
    pattern encodes. Daemon code in `slack_socket` consumes the result
    and either threads the fields into `pending_approvals.resolve(...)`
    (decision/modal paths) or drives `expandable_messages` lookup +
    `chat.update` (toggle path).
    """
    raw: str
    decision: str | None = None  # "allow" | "deny"
    modal_kind: str | None = None  # "custom_answer" | "deny_reason"
    toggle_kind: str | None = None  # "expand" | "collapse"
    selected_question: int | None = None
    selected_option: int | None = None
    selected_suggestion: int | None = None
    modal_question_index: int | None = None


def parse_action_id(action_id: str) -> ParsedActionId | None:
    """Decode an `agent_notify_*` action_id string from a Slack click.

    Returns None if the string doesn't match any of our patterns
    (including malformed numeric suffixes — the wire schema is small,
    so anything off-pattern is treated as not-ours and dropped).

    Wire schema (must stay byte-stable for back-compat):
      - `agent_notify_approve`           → allow
      - `agent_notify_deny`              → deny
      - `agent_notify_deny_reason`       → opens deny-with-reason modal
      - `agent_notify_custom_answer_<q>` → opens custom-answer modal for Q<q>
      - `agent_notify_suggestion_<i>`    → allow + apply suggestion <i>
      - `agent_notify_option_<o>`        → allow + select option <o> on Q0 (legacy)
      - `agent_notify_option_<q>_<o>`    → allow + select option <o> on Q<q>
      - `agent_notify_expand`            → swap message to full body (msg_id in `value`)
      - `agent_notify_collapse`          → swap message to preview body (msg_id in `value`)
    """
    if action_id == APPROVE_ACTION_ID:
        return ParsedActionId(raw=action_id, decision="allow")
    if action_id == DENY_ACTION_ID:
        return ParsedActionId(raw=action_id, decision="deny")
    if action_id == DENY_REASON_ACTION_ID:
        return ParsedActionId(raw=action_id, modal_kind="deny_reason")
    if action_id == EXPAND_ACTION_ID:
        return ParsedActionId(raw=action_id, toggle_kind="expand")
    if action_id == COLLAPSE_ACTION_ID:
        return ParsedActionId(raw=action_id, toggle_kind="collapse")
    if action_id.startswith(CUSTOM_ANSWER_ACTION_ID_PREFIX):
        try:
            q_idx = int(action_id[len(CUSTOM_ANSWER_ACTION_ID_PREFIX):])
        except ValueError:
            return None
        return ParsedActionId(
            raw=action_id, modal_kind="custom_answer", modal_question_index=q_idx
        )
    if action_id.startswith(SUGGESTION_ACTION_ID_PREFIX):
        try:
            s_idx = int(action_id[len(SUGGESTION_ACTION_ID_PREFIX):])
        except ValueError:
            return None
        return ParsedActionId(
            raw=action_id, decision="allow", selected_suggestion=s_idx
        )
    if action_id.startswith(OPTION_ACTION_ID_PREFIX):
        suffix = action_id[len(OPTION_ACTION_ID_PREFIX):]
        parts = suffix.split("_")
        try:
            if len(parts) == 1:
                # Legacy single-question encoding — treat as question 0.
                return ParsedActionId(
                    raw=action_id, decision="allow",
                    selected_question=0, selected_option=int(parts[0]),
                )
            if len(parts) == 2:
                return ParsedActionId(
                    raw=action_id, decision="allow",
                    selected_question=int(parts[0]),
                    selected_option=int(parts[1]),
                )
        except ValueError:
            return None
        return None
    return None
# Slack button text limit; truncate option labels to fit.
_BUTTON_TEXT_MAX = 75
# Slack modal title hard cap (24 chars per Block Kit spec).
_MODAL_TITLE_MAX = 24
# Slack plain_text_input max_length cap we expose to users.
_MODAL_INPUT_MAX = 1000
# Truncate freeform answer text in resolved-message render so a 1000-char
# rant doesn't blow out the chat.update payload.
_RESOLVED_TEXT_MAX = 80

# Slack attachment color bar per event kind. Tier semantics:
#   green  = informational / done           (turn_complete, idle_prompt, elicitation)
#   yellow = action required / approval     (permission)
#   red    = danger override (set when      (DANGEROUS_BASH_PATTERNS hit on tool_input)
#            the rendered tool call hits
#            DANGEROUS_BASH_PATTERNS)
_GREEN = "#2eb67d"
_YELLOW = "#ecb22e"
_KIND_COLORS: dict[EventKind, str] = {
    "permission": _YELLOW,
    "idle_prompt": _GREEN,
    "elicitation": _GREEN,
    "turn_complete": _GREEN,
}
_DANGER_COLOR = "#a30f18"


@dataclass
class SlackSink:
    config: SlackConfig
    name: str = "slack"
    tool_input_max_chars: int = DEFAULT_MAX_CHARS
    verbosity: Verbosity = "normal"
    message_max_chars: int = 0
    message_preview_head_chars: int = 0
    message_preview_tail_chars: int = 0
    workspace: str = "default"

    def send(self, event: Event) -> None:
        if not self.config.enabled:
            return
        full_body = build_slack_message(
            event,
            max_chars=self.tool_input_max_chars,
            verbosity=self.verbosity,
            message_max_chars=self.message_max_chars,
        )
        if self.config.webhook_url:
            # Webhooks can't chat.update, so the toggle is unavailable — post
            # the full body and rely on Slack's auto-collapse (such as it is).
            status, text = http_post_json(self.config.webhook_url, full_body)
            if status >= 300 or text.strip() not in ("ok", ""):
                raise SinkError(f"Slack webhook returned {status}: {text!r}")
            return
        if not self.config.bot_token:
            raise SinkError("Slack sink has neither webhook_url nor bot_token configured")

        # Decide whether to wrap in a Show more / Show less toggle. Skip when:
        #   - minimal verbosity hides the body anyway,
        #   - user set a hard cap (message_max_chars > 0) so the truncated
        #     output is the intended final form,
        #   - both preview budgets are 0 (toggle disabled),
        #   - the preview snippet equals the full message (it fit, no truncation).
        preview_text: str | None = None
        if (
            self.verbosity != "minimal"
            and self.message_max_chars == 0
            and event.message
            and (self.message_preview_head_chars > 0 or self.message_preview_tail_chars > 0)
        ):
            preview_text = _build_preview_text(
                event.message,
                head_chars=self.message_preview_head_chars,
                tail_chars=self.message_preview_tail_chars,
            )

        message_id = uuid.uuid4().hex if preview_text is not None else None
        if message_id is not None and preview_text is not None:
            preview_event = dataclasses.replace(event, message=preview_text)
            preview_body = build_slack_message(
                preview_event,
                max_chars=self.tool_input_max_chars,
                verbosity=self.verbosity,
                message_max_chars=self.message_max_chars,
            )
            _append_block_to_first_attachment(
                preview_body,
                _toggle_actions_block(EXPAND_ACTION_ID, "Show more", message_id),
            )
            _append_block_to_first_attachment(
                full_body,
                _toggle_actions_block(COLLAPSE_ACTION_ID, "Show less", message_id),
            )
            body_to_post = preview_body
        else:
            body_to_post = full_body

        channel = self.config.channel or "@me"
        if channel == "@me":
            channel = _dm_target(self.config)
        payload = {"channel": channel, **body_to_post}
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

        if message_id is not None:
            ts = parsed.get("ts")
            posted_channel = parsed.get("channel") or channel
            if isinstance(ts, str) and ts:
                expandable_messages.create(
                    message_id,
                    workspace=self.workspace,
                    channel=posted_channel,
                    message_ts=ts,
                    preview_body=body_to_post,
                    full_body=full_body,
                )


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
    message_max_chars: int = 0,
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

    # AskUserQuestion is a question, not a permission warning — render with
    # green/thinking-face styling so it doesn't read as a destructive-action
    # alert. Other tools keep their kind's emoji + title.
    if event.tool_name == "AskUserQuestion":
        emoji = ":thinking_face:"
        title = f"{AGENT_LABELS[event.agent]} is asking"
    else:
        emoji = event.emoji
        title = event.title
    header = f"{':rotating_light: ' if dangerous else ''}{emoji} {title}"
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

    body_chunks = _compose_body(event, tool, verbosity, message_max_chars=message_max_chars)
    for i, chunk in enumerate(body_chunks):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": chunk},
            # Tag for `_try_build_preview_body`. Slack requires unique block_ids
            # per message, so suffix with the chunk index.
            "block_id": f"agent_notify_body_msg_{i}",
        })
    if tool.detail:
        if tool.code_block:
            lang = tool.code_block_lang
            fence = f"```{lang}\n" if lang else "```\n"
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{fence}{tool.detail}\n```"},
                "block_id": "agent_notify_body_detail_0",
            })
        else:
            # Non-fence detail (ExitPlanMode plan, prose) — convert markdown
            # to Slack mrkdwn and chunk across multiple section blocks if
            # over the platform cap. Without this, a 5000-char plan would
            # exceed Slack's 3000-char section-block limit and fail to post.
            polished = _mrkdwn_polish(tool.detail)
            for i, chunk in enumerate(chunk_text(
                polished,
                chunk_size=_SECTION_CHUNK_SIZE,
                max_chars=message_max_chars,
            )):
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": chunk},
                    "block_id": f"agent_notify_body_detail_{i}",
                })

    if verbosity == "terse":
        footer = _terse_footer(event)
        if footer:
            blocks.append(
                {"type": "context",
                 "elements": [{"type": "mrkdwn", "text": footer}]}
            )

    if dangerous:
        color = _DANGER_COLOR
    elif event.tool_name == "AskUserQuestion":
        # Override the kind's yellow to green — this is a question, not an
        # approval. Matches the green header above.
        color = _GREEN
    else:
        color = _KIND_COLORS.get(event.kind, "")
    attachment = {
        "color": color,
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


def _compose_body(
    event: Event,
    tool: ToolRender,
    verbosity: Verbosity,
    *,
    message_max_chars: int = 0,
) -> list[str]:
    """Return the section-block body texts.

    `event.message` is split across multiple chunks when over Slack's 3000-char
    section cap (or `message_max_chars` if positive). The tool summary is
    appended to the LAST message chunk so terse-mode `*Bash:* echo hi` styling
    stays adjacent to the body.
    """
    summary = tool.summary
    if verbosity == "terse" and event.tool_name and summary:
        summary = f"*{event.tool_name}:* {summary}"
    elif verbosity == "terse" and event.tool_name and not event.message and not summary:
        summary = f"*{event.tool_name}*"

    chunks: list[str] = []
    if event.message:
        chunks = chunk_text(
            _mrkdwn_polish(event.message),
            chunk_size=_SECTION_CHUNK_SIZE,
            max_chars=message_max_chars,
        )
    if summary:
        if chunks:
            joined = chunks[-1] + "\n" + summary
            if len(joined) <= _SECTION_CHUNK_SIZE:
                chunks[-1] = joined
            else:
                chunks.append(summary)
        else:
            chunks.append(summary)
    return chunks


def _build_preview_text(text: str, *, head_chars: int, tail_chars: int) -> str | None:
    """Return a head…tail preview of `text`, or None if no truncation is needed.

    Returns None when the message body fits within the preview budget — that's
    the signal to `SlackSink.send` that no Show more button should be added.
    """
    if head_chars <= 0 and tail_chars <= 0:
        return None
    snippet = transcript.head_tail_snippet(text, head=head_chars, tail=tail_chars)
    if snippet == text.strip():
        return None
    return snippet


def _toggle_actions_block(action_id: str, label: str, message_id: str) -> dict:
    """Build a single-button actions block for the Show more / Show less toggle."""
    return {
        "type": "actions",
        "elements": [{
            "type": "button",
            "action_id": action_id,
            "text": {"type": "plain_text", "text": label, "emoji": True},
            "value": message_id,
        }],
    }


def _append_block_to_first_attachment(body: dict, block: dict) -> None:
    """Append a block to the first attachment so the color bar stays intact."""
    attachments = body.get("attachments") or []
    if attachments:
        attachments[0].setdefault("blocks", []).append(block)
    else:
        body.setdefault("blocks", []).append(block)


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


def _mrkdwn_polish(text: str) -> str:
    """Convert standard markdown to Slack mrkdwn (bold, italic, headings,
    links, lists, strike) and inline-code-format any /paths/. Delegates to
    `sinks.mrkdwn.to_slack_mrkdwn` for the markdown rules; the path rule
    runs as part of that pipeline. Code fences and inline backticks are
    protected so markdown syntax inside code stays literal."""
    return _mrkdwn.to_slack_mrkdwn(text)


_IOS_PREVIEW_MAX = 140


def _fallback_text(event: Event, tool: ToolRender, dangerous: bool, *,
                   verbosity: Verbosity = "normal") -> str:
    """Compose the Slack `text` field — what iOS renders in the push preview.

    Slack mobile also displays this above the attachment in-app, so a full-body
    dump produces visible duplication. Keep it to a single line, capped at
    140 chars; the rich content still lives in the attachment blocks. In
    `minimal` verbosity the preview is just the event title — no command,
    no code.

    The summary runs through `_mrkdwn_polish` BEFORE single-line collapse so
    `## heading` / `**bold**` / `[link](url)` render correctly in Slack's
    in-app preview line. Push notifications strip the markers, leaving
    plain text on the lock screen.
    """
    if verbosity == "minimal":
        return event.title
    bits = []
    if dangerous:
        bits.append("⚠️ DANGEROUS")
    bits.append(event.title)
    summary = tool.summary or event.message
    if summary:
        polished = _mrkdwn_polish(summary)
        bits.append(_single_line(polished, _IOS_PREVIEW_MAX))
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


def _ask_user_question_questions(
    tool_name: str | None,
    tool_input: dict | None,
) -> list[dict] | None:
    """Return AskUserQuestion's questions list, or None for non-AUQ / malformed.

    Each question is a dict with at least `question` (str), `options`
    (list of {label, description?}). `multiSelect` and `header` are
    optional. The slack renderer iterates this list — questions with
    `multiSelect: true` are surfaced as text-only (no buttons), since
    Slack actions can't carry multi-select state cleanly.
    """
    if tool_name != "AskUserQuestion" or not isinstance(tool_input, dict):
        return None
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    valid: list[dict] = []
    for q in questions:
        if not isinstance(q, dict):
            return None
        if not isinstance(q.get("question"), str):
            return None
        options = q.get("options")
        if not isinstance(options, list) or not options:
            return None
        # Validate that every option has a string label so the renderer
        # doesn't crash mid-build.
        for opt in options:
            if not isinstance(opt, dict) or not isinstance(opt.get("label"), str):
                return None
        valid.append(q)
    return valid


def _ask_user_question_options(
    tool_name: str | None,
    tool_input: dict | None,
) -> list[str] | None:
    """Back-compat shim: returns option labels of `questions[0]` only.

    Kept so the legacy single-question path (and its tests) still resolves.
    Use `_ask_user_question_questions` for multi-question rendering.
    """
    questions = _ask_user_question_questions(tool_name, tool_input)
    if not questions:
        return None
    q = questions[0]
    if q.get("multiSelect") is True:
        return None
    return [opt["label"] for opt in q["options"]]


def _selected_label_from_record(rec: dict) -> str | None:
    """Look up the selected option's label from a resolved approval record.

    Prefers freeform_answers (typed text via custom-answer modal) over
    selected_options (button click) over the legacy selected_option_index.
    Returns the typed text directly when it wins — the resolved-message
    header reads "Selected `<text>`" the same way it would for a label.
    """
    questions = _ask_user_question_questions(rec.get("tool_name"), rec.get("tool_input"))
    if not questions:
        return None
    freeform = rec.get("freeform_answers")
    if isinstance(freeform, dict) and isinstance(freeform.get("0"), str):
        return freeform["0"]
    selected_options = rec.get("selected_options")
    if isinstance(selected_options, dict) and "0" in selected_options:
        idx = selected_options["0"]
        if isinstance(idx, int) and 0 <= idx < len(questions[0]["options"]):
            return questions[0]["options"][idx]["label"]
    idx = rec.get("selected_option_index")
    if isinstance(idx, int) and 0 <= idx < len(questions[0]["options"]):
        return questions[0]["options"][idx]["label"]
    return None


def _truncate_button_text(text: str) -> str:
    if len(text) <= _BUTTON_TEXT_MAX:
        return text
    return text[: _BUTTON_TEXT_MAX - 1] + "…"  # …


def _build_option_buttons_for_question(
    approval_id: str,
    question_index: int,
    labels: list[str],
    *,
    answered_index: int | None = None,
    answered_freeform: bool = False,
) -> dict:
    """One actions block for a single question: option buttons indexed by
    `question_index`, plus a trailing "✏️ Custom answer" trigger that opens
    a modal. Each option's `action_id` is `agent_notify_option_<q>_<o>` so
    the daemon knows which question the click belongs to; the custom-answer
    button uses `agent_notify_custom_answer_<q>`.

    The first option whose label contains "(Recommended)" gets
    `style: "primary"` (filled green CTA) — Slack convention is one
    primary per actions block. The custom-answer button stays unstyled to
    scan as a fallback, not a CTA.

    `answered_index`, when provided, tags the already-clicked option's
    button with a check-mark prefix. `answered_freeform=True` tags the
    custom-answer button instead (the user typed text). Either way, the
    other buttons stay tappable so the user can switch their mind before
    the approval finalizes (rare — finalize fires the moment every question
    has an entry).
    """
    primary_assigned = False
    elements: list[dict] = []
    for i, label in enumerate(labels):
        text = label
        if answered_index == i:
            text = f"✓ {label}"
        button: dict = {
            "type": "button",
            "text": {"type": "plain_text", "text": _truncate_button_text(text), "emoji": False},
            "action_id": f"{OPTION_ACTION_ID_PREFIX}{question_index}_{i}",
            "value": approval_id,
        }
        if not primary_assigned and "(Recommended)" in label:
            button["style"] = "primary"
            primary_assigned = True
        elements.append(button)
    # Reserve one slot for the custom-answer button when within Slack's
    # 25-element actions cap.
    if len(elements) > 24:
        elements = elements[:24]
    custom_text = "✓ ✏️ Custom answer" if answered_freeform else "✏️ Custom answer"
    elements.append({
        "type": "button",
        "text": {"type": "plain_text", "text": custom_text, "emoji": True},
        "action_id": f"{CUSTOM_ANSWER_ACTION_ID_PREFIX}{question_index}",
        "value": approval_id,
    })
    return {
        "type": "actions",
        "block_id": f"agent_notify::{approval_id}::q{question_index}",
        "elements": elements,
    }


def _build_deny_block(approval_id: str) -> dict:
    return {
        "type": "actions",
        "block_id": f"agent_notify::{approval_id}::deny",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Deny", "emoji": False},
                "style": "danger",
                "action_id": DENY_ACTION_ID,
                "value": approval_id,
            },
            _build_deny_with_reason_button(approval_id),
        ],
    }


def _build_deny_with_reason_button(approval_id: str) -> dict:
    """Trigger button for the deny-with-reason modal. Sits next to the
    one-tap Deny — both `style: danger` so the deny semantics scan
    visually together. Tap → modal with a single text input → submit
    resolves with `deny_reason` plumbed into `decision.message`.
    """
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": "💬 Deny with reason", "emoji": True},
        "style": "danger",
        "action_id": DENY_REASON_ACTION_ID,
        "value": approval_id,
    }


def _modal_title(text: str, fallback: str) -> str:
    """Slack view titles cap at 24 chars; truncate or fall back."""
    flat = " ".join(text.split())
    if not flat:
        return fallback
    if len(flat) <= _MODAL_TITLE_MAX:
        return flat
    return flat[: _MODAL_TITLE_MAX - 1] + "…"


def build_custom_answer_modal(
    approval_id: str,
    question_index: int,
    question_text: str,
    *,
    initial_value: str | None = None,
) -> dict:
    """Slack view payload for the "✏️ Custom answer" modal.

    `private_metadata` carries the JSON `{approval_id, question_index}` so
    the daemon's view_submission handler can route the typed text back to
    the right question. `initial_value` pre-fills the field — used when
    the user re-opens the modal after a previous submission.
    """
    metadata = json.dumps({"approval_id": approval_id, "question_index": question_index})
    element: dict = {
        "type": "plain_text_input",
        "action_id": MODAL_INPUT_ACTION_ID,
        "multiline": True,
        "min_length": 1,
        "max_length": _MODAL_INPUT_MAX,
        "placeholder": {"type": "plain_text", "text": "Your answer…"},
    }
    if initial_value:
        element["initial_value"] = initial_value
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK_CUSTOM_ANSWER,
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": _modal_title(question_text, "Custom answer")},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{question_text}*"},
            },
            {
                "type": "input",
                "block_id": MODAL_INPUT_BLOCK_ID,
                "label": {"type": "plain_text", "text": "Your answer"},
                "element": element,
            },
        ],
    }


def build_deny_reason_modal(approval_id: str) -> dict:
    """Slack view payload for the "💬 Deny with reason" modal.

    The typed text becomes `decision.message` on the deny path — Claude
    sees it as the rejection reason and can adjust its approach.
    """
    metadata = json.dumps({"approval_id": approval_id})
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK_DENY_REASON,
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": "Why deny?"},
        "submit": {"type": "plain_text", "text": "Deny"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": MODAL_INPUT_BLOCK_ID,
                "label": {"type": "plain_text", "text": "Reason for denial"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": MODAL_INPUT_ACTION_ID,
                    "multiline": True,
                    "min_length": 1,
                    "max_length": _MODAL_INPUT_MAX,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Why is this denied? Claude will see this.",
                    },
                },
            },
        ],
    }


def extract_modal_text(view: dict) -> str | None:
    """Pull the user's typed string out of a `view_submission` view.

    Returns the input value at `view.state.values[MODAL_INPUT_BLOCK_ID][MODAL_INPUT_ACTION_ID]`,
    or None if the shape doesn't match (defensive against Slack schema drift).
    """
    if not isinstance(view, dict):
        return None
    state = view.get("state")
    if not isinstance(state, dict):
        return None
    values = state.get("values")
    if not isinstance(values, dict):
        return None
    block = values.get(MODAL_INPUT_BLOCK_ID)
    if not isinstance(block, dict):
        return None
    inp = block.get(MODAL_INPUT_ACTION_ID)
    if not isinstance(inp, dict):
        return None
    val = inp.get("value")
    return val if isinstance(val, str) and val else None


def _build_multi_question_blocks(
    approval_id: str,
    questions: list[dict],
    *,
    selected_options: dict[str, int] | None = None,
    freeform_answers: dict[str, str] | None = None,
) -> list[dict]:
    """Block Kit blocks for an N-question AskUserQuestion: a section header
    per question (with ✓ once answered) followed by its option buttons +
    a "✏️ Custom answer" trigger, then a single trailing Deny block at the
    end. multiSelect questions render as text-only ("answer this in the
    terminal") since Slack buttons can't carry multi-select state cleanly.

    `freeform_answers` (str_q_idx → typed text) wins over `selected_options`
    per question — if the user submitted a custom answer modal for Q1, the
    section header shows the typed text, the custom-answer button gets a ✓,
    and the option buttons stay un-checked.
    """
    selected = selected_options or {}
    freeform = freeform_answers or {}
    blocks: list[dict] = []
    for q_idx, q in enumerate(questions):
        freeform_text = freeform.get(str(q_idx)) if isinstance(freeform.get(str(q_idx)), str) else None
        answered_opt = None if freeform_text else q_idx_str_in_dict(selected, q_idx)
        check = "✓" if (freeform_text is not None or answered_opt is not None) else " "
        header = q.get("header") or q["question"]
        section_text = f"*{check} Q{q_idx + 1}.* {header}"
        if freeform_text is not None:
            section_text += f"\n_Answered: \"{_truncate_resolved_text(freeform_text)}\"_"
        elif answered_opt is not None and 0 <= answered_opt < len(q["options"]):
            section_text += f"\n_Answered: {q['options'][answered_opt]['label']}_"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": section_text},
        })
        if q.get("multiSelect") is True:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn",
                              "text": "_multi-select question — answer in the terminal._"}],
            })
            continue
        labels = [opt["label"] for opt in q["options"]]
        blocks.append(_build_option_buttons_for_question(
            approval_id, q_idx, labels,
            answered_index=answered_opt,
            answered_freeform=freeform_text is not None,
        ))
    blocks.append(_build_deny_block(approval_id))
    return blocks


def _truncate_resolved_text(text: str) -> str:
    """Cap freeform answer / deny reason text shown in re-rendered messages."""
    flat = " ".join(text.split())
    if len(flat) <= _RESOLVED_TEXT_MAX:
        return flat
    return flat[: _RESOLVED_TEXT_MAX - 1].rstrip() + "…"


def q_idx_str_in_dict(selected: dict[str, int], q_idx: int) -> int | None:
    """Return the option index recorded for question `q_idx`, or None."""
    val = selected.get(str(q_idx))
    return val if isinstance(val, int) else None


def _suggestion_label(suggestion: dict) -> str:
    """Derive a human button label from a permission_suggestion dict.

    Suggestions are shaped like:
      {"type": "addRules",
       "rules": [{"toolName": "Bash", "ruleContent": "npm test"}],
       "behavior": "allow",
       "destination": "localSettings"}

    The label summarizes intent: "Approve & add Bash(npm test) to
    localSettings". Falls back to a generic label if the shape is
    unfamiliar so we never crash on a future schema variant.
    """
    behavior = suggestion.get("behavior", "allow")
    destination = suggestion.get("destination", "settings")
    rules = suggestion.get("rules") or []
    if isinstance(rules, list) and rules and isinstance(rules[0], dict):
        rule = rules[0]
        tool = rule.get("toolName") or "tool"
        content = rule.get("ruleContent") or ""
        rule_summary = f"{tool}({content})" if content else tool
        verb = "Approve & add" if behavior == "allow" else "Approve & deny"
        return f"{verb} `{rule_summary}` to {destination}"
    return f"Approve with {behavior}/{destination}"


def _build_suggestion_buttons(approval_id: str, suggestions: list[dict]) -> dict:
    """Block Kit actions block with one button per suggestion. action_id
    pattern is `agent_notify_suggestion_<index>` so the daemon can look
    up which suggestion was clicked.
    """
    elements: list[dict] = []
    for i, suggestion in enumerate(suggestions):
        elements.append({
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": _truncate_button_text(_suggestion_label(suggestion)),
                "emoji": False,
            },
            "action_id": f"{SUGGESTION_ACTION_ID_PREFIX}{i}",
            "value": approval_id,
        })
    if len(elements) > 25:
        elements = elements[:25]
    return {
        "type": "actions",
        "block_id": f"agent_notify::{approval_id}::sugg",
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
            _build_deny_with_reason_button(approval_id),
        ],
    }


def build_approval_message(
    event: Event,
    approval_id: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    verbosity: Verbosity = "terse",
    message_max_chars: int = 0,
    selected_options: dict[str, int] | None = None,
    freeform_answers: dict[str, str] | None = None,
) -> dict:
    """Like `build_slack_message` but with an actions block for the user.

    For most tools, two buttons: Approve (primary, with confirm dialog so an
    accidental lock-screen tap doesn't run a Bash command) and Deny (danger,
    no confirm — fail-safe direction).

    For AskUserQuestion, one actions block per question (option buttons
    labeled from each option) plus a trailing Deny block. `selected_options`
    (dict[str_question_index, option_index]) renders ✓ marks on already-
    clicked options — the daemon passes this into chat.update calls during
    the multi-question flow so the message reflects partial progress.
    Falls back to Approve/Deny if the tool isn't AskUserQuestion or the
    payload is malformed.
    """
    body = build_slack_message(
        event,
        max_chars=max_chars,
        verbosity=verbosity,
        message_max_chars=message_max_chars,
    )

    questions = _ask_user_question_questions(event.tool_name, event.tool_input)
    if questions:
        # One actions block per question + a single trailing Deny.
        # AskUserQuestion never gets suggestion buttons — the option
        # buttons ARE the answer; suggestions would conflict.
        appended_blocks = _build_multi_question_blocks(
            approval_id, questions,
            selected_options=selected_options,
            freeform_answers=freeform_answers,
        )
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
        appended_blocks = [_build_approve_deny_block(approval_id, confirm_text)]
        # Append per-suggestion buttons after Approve/Deny. Tapping one
        # equals "approve AND apply this rule edit" — the user gets the
        # extra-allowlist outcome in a single tap. Suppressed in minimal
        # verbosity (the rule content would leak the tool input).
        if event.permission_suggestions and verbosity != "minimal":
            appended_blocks.append(
                _build_suggestion_buttons(approval_id, list(event.permission_suggestions))
            )

    # Append to the first attachment so the color bar / fallback text stay
    # intact. Falls back to top-level blocks if the builder ever stops using
    # attachments (defensive).
    if body.get("attachments"):
        body["attachments"][0].setdefault("blocks", []).extend(appended_blocks)
    else:
        body.setdefault("blocks", []).extend(appended_blocks)
    return body


def post_approval_message(
    event: Event,
    slack_config: SlackConfig,
    approval_id: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    verbosity: Verbosity = "terse",
    message_max_chars: int = 0,
    message_preview_head_chars: int = 0,
    message_preview_tail_chars: int = 0,
    workspace: str = "default",
    poster: "callable | None" = None,
) -> tuple[str, str]:
    """Post an approval message, return (channel_id, message_ts).

    When `message_preview_head_chars + message_preview_tail_chars > 0` AND
    the rendered body has a section block longer than that budget (e.g. an
    ExitPlanMode plan), the toggle pair is persisted via
    `expandable_messages.create` so the daemon can swap views on Show more /
    Show less click. The Approve / Deny / suggestion buttons stay live in
    both states.

    `poster` is injectable for tests (matches existing `_FakePoster` pattern in
    test_sinks.py). Production passes the default `http_post_json`.
    """
    if not slack_config.bot_token:
        raise SinkError("approval messages require bot_token (webhook cannot carry interactivity)")
    full_body = build_approval_message(
        event,
        approval_id,
        max_chars=max_chars,
        verbosity=verbosity,
        message_max_chars=message_max_chars,
    )

    # Build (preview, full) pair when the body has a long section block AND
    # the toggle isn't disabled by minimal/hard-cap/zero-budget. The toggle
    # never activates for AskUserQuestion (its bodies are short); it's
    # primarily for ExitPlanMode plans.
    preview_body = None
    message_id = None
    if (
        verbosity != "minimal"
        and message_max_chars == 0
        and (message_preview_head_chars > 0 or message_preview_tail_chars > 0)
    ):
        preview_candidate = _try_build_preview_body(
            full_body,
            head_chars=message_preview_head_chars,
            tail_chars=message_preview_tail_chars,
        )
        if preview_candidate is not None:
            message_id = uuid.uuid4().hex
            _append_block_to_first_attachment(
                preview_candidate,
                _toggle_actions_block(EXPAND_ACTION_ID, "Show more", message_id),
            )
            _append_block_to_first_attachment(
                full_body,
                _toggle_actions_block(COLLAPSE_ACTION_ID, "Show less", message_id),
            )
            preview_body = preview_candidate

    body_to_post = preview_body if preview_body is not None else full_body
    channel = slack_config.channel or "@me"
    if channel == "@me":
        channel = _dm_target(slack_config, poster=poster)
    payload = {"channel": channel, **body_to_post}
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

    if message_id is not None and preview_body is not None:
        expandable_messages.create(
            message_id,
            workspace=workspace,
            channel=posted_channel,
            message_ts=ts,
            preview_body=preview_body,
            full_body=full_body,
        )
    return posted_channel, ts


def _try_build_preview_body(full_body: dict, *, head_chars: int, tail_chars: int) -> dict | None:
    """Return a deep-copy of `full_body` with body section blocks collapsed
    to a single head…tail snippet, when the combined body exceeds budget.

    "Body" here means: the first run of consecutive section blocks tagged
    `block_id="agent_notify_body"`, which `build_slack_message` uses for
    both event.message chunks and non-fence tool.detail chunks. Multi-chunk
    bodies (e.g. an 8000-char plan split across 3 section blocks) are
    treated as one logical body and replaced by ONE elided block — not
    "elided chunk 1 + full chunks 2 and 3", which would defeat the toggle.

    Returns None when no body block exceeds budget (toggle not needed).
    """
    import copy as _copy
    attachments = full_body.get("attachments") or []
    if not attachments:
        return None
    blocks = attachments[0].get("blocks") or []

    # Find the first run of body blocks.
    start = -1
    end = -1
    for idx, blk in enumerate(blocks):
        is_body = (
            blk.get("type") == "section"
            and isinstance(blk.get("block_id"), str)
            and blk["block_id"].startswith("agent_notify_body_")
            and "text" in blk
        )
        if is_body and start < 0:
            start = idx
            end = idx
        elif is_body:
            end = idx
        elif start >= 0:
            break  # run ended

    if start < 0:
        return None

    combined = "\n\n".join(
        blocks[i]["text"].get("text", "") for i in range(start, end + 1)
    )
    snippet = transcript.head_tail_snippet(combined, head=head_chars, tail=tail_chars)
    if snippet == combined.strip():
        return None  # body fits within budget — no toggle needed

    preview = _copy.deepcopy(full_body)
    preview_blocks = preview["attachments"][0]["blocks"]
    # Replace the first body block with the elided snippet, drop the rest.
    preview_blocks[start]["text"]["text"] = snippet
    del preview_blocks[start + 1 : end + 1]
    return preview


def build_resolved_message(
    event: Event,
    decision: str,
    actor_label: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    verbosity: Verbosity = "terse",
    selected_label: str | None = None,
    selected_options: dict[str, int] | None = None,
    freeform_answers: dict[str, str] | None = None,
    selected_suggestion_label: str | None = None,
    deny_reason: str | None = None,
) -> dict:
    """Block Kit replacement for `chat.update` after approve/deny or timeout.

    Preserves the tool summary so scroll-back context isn't lost, but strips
    the buttons and swaps the header to the outcome.

    Args:
      selected_label: legacy single-question AskUserQuestion — header reads
        "Selected `<label>` by @user". Pass the typed text here for the
        single-question custom-answer case.
      selected_options: multi-question AskUserQuestion — header reads
        "Answered" and a section block lists each Q→A pair.
      freeform_answers: per-question typed text from custom-answer modals.
        Wins over `selected_options` per question in the Q→A summary.
      selected_suggestion_label: a permission_suggestion was clicked —
        header reads "Approved & applied: <label> by @user".
      deny_reason: typed text from the deny-with-reason modal. Rendered in
        a context block under the denied header.

    Decision is still "allow" for the answer/suggestion variants; the
    selection both approves the tool call AND chooses the answer(s) /
    rule edit.
    """
    has_multi = bool(selected_options) or bool(freeform_answers)
    if decision == "allow":
        icon = ":white_check_mark:"
        verb = "Approved"
        color = "#2eb67d"
        if selected_suggestion_label:
            verb = f"Approved & applied {selected_suggestion_label}"
        elif has_multi:
            verb = "Answered"
        elif selected_label:
            verb = f"Selected `{_truncate_resolved_text(selected_label)}`"
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
    if decision == "deny" and deny_reason:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"_Reason: \"{_truncate_resolved_text(deny_reason)}\"_",
            }],
        })

    if verbosity == "minimal":
        # Outcome + actor only — no tool name, no context footer. Reason
        # text already added above for deny.
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

    # Multi-question Q→A summary block. Renders one mrkdwn line per
    # answered question; freeform text wins over option label per question.
    if has_multi:
        questions = _ask_user_question_questions(event.tool_name, event.tool_input)
        if questions:
            options_dict = selected_options or {}
            freeform_dict = freeform_answers or {}
            lines: list[str] = []
            for q_idx, q in enumerate(questions):
                key = str(q_idx)
                ftext = freeform_dict.get(key) if isinstance(freeform_dict.get(key), str) else None
                if ftext is not None:
                    lines.append(
                        f"*Q{q_idx + 1}.* {q['question']}\n→ \"{_truncate_resolved_text(ftext)}\""
                    )
                    continue
                ans_idx = q_idx_str_in_dict(options_dict, q_idx)
                if ans_idx is not None and 0 <= ans_idx < len(q["options"]):
                    label = q["options"][ans_idx]["label"]
                    lines.append(f"*Q{q_idx + 1}.* {q['question']}\n→ `{label}`")
                else:
                    lines.append(f"*Q{q_idx + 1}.* {q['question']}\n→ _(no answer)_")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n\n".join(lines)},
            })

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
