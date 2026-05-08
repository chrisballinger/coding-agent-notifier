"""Slack Socket Mode daemon + button-click handler.

The daemon opens a WebSocket from this machine out to Slack (no inbound
port). One thread per configured workspace owns its own WebSocket; each
thread's listener only sees clicks from its own workspace, so a click
is always resolved with the same bot that posted the message.

Interactive payloads — approve/deny button clicks — arrive over the WS.
The handler resolves the matching `PendingApproval`, which unblocks the
`PreToolUse` hook that was waiting on the FIFO, and edits the original
message in place to show the outcome.

`handle_block_actions` is pure-ish (side effects injected) so tests can
exercise the decision logic without `slack_sdk` or a real WS. `run_daemon`
is the thin wrapper that imports `slack_sdk` and loops.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import expandable_messages, pending_approvals
from .config import Config, SlackConfig
from .event import Event
from .sinks.slack import (
    MODAL_CALLBACK_CUSTOM_ANSWER,
    MODAL_CALLBACK_DENY_REASON,
    _ask_user_question_questions,
    _selected_label_from_record,
    _suggestion_label,
    build_approval_message,
    build_custom_answer_modal,
    build_deny_reason_modal,
    build_resolved_message,
    extract_modal_text,
    parse_action_id,
    update_message,
)

logger = logging.getLogger(__name__)

# Slack DM channel IDs start with "D" (e.g. "D12345ABC"). Used in the
# empty-allowlist fallback to verify the click actually came from a DM
# rather than a shared channel — defense-in-depth against misconfigured
# channels or weird Slack routing.
_DM_CHANNEL_PREFIX = "D"

# Shown when an approver clicks a stale Custom-answer / Deny-with-reason
# button (or a stale modal somehow submits) on a workspace whose
# freeform_text policy is set to "deny".
_FREEFORM_DISABLED_HINT = (
    ":lock: Freeform text entry is disabled for this workspace. "
    "Use the approve / deny / option buttons on the original message."
)


def _post_freeform_disabled_hint(
    *,
    channel: str | None,
    user: str | None,
    ephemeral_fn: Callable[..., None] | None,
) -> None:
    """Best-effort ephemeral hint when a freeform-disabled action arrives.

    Hooks-must-never-block applies: any failure (missing channel, missing
    user, ephemeral_fn raising) is logged and swallowed — the approval
    record stays untouched so the user can still click a fixed-option
    button.
    """
    if ephemeral_fn is None or not channel or not user:
        return
    try:
        ephemeral_fn(channel=channel, user=user, text=_FREEFORM_DISABLED_HINT)
    except Exception:
        logger.exception("failed to post freeform-disabled hint to %s", user)


@dataclass
class ButtonClickResult:
    handled: bool
    decision: str | None
    rejected_reason: str | None
    approval_id: str | None
    user_id: str | None


def handle_block_actions(
    payload: dict[str, Any],
    slack_config: SlackConfig,
    *,
    workspace: str = "default",
    resolve_fn: Callable[..., dict | None] = pending_approvals.resolve,
    ephemeral_fn: Callable[..., None] | None = None,
    update_fn: Callable[..., None] = update_message,
    views_open_fn: Callable[[str, dict], None] | None = None,
    group_member_check: Callable[[str, str], bool] | None = None,
    base_dir: Path | None = None,
) -> ButtonClickResult:
    """Act on a Slack `block_actions` payload.

    Authorization order (first match wins):
      1. `user_id` in `approver_user_ids`.
      2. `user_id` is a member of any group in `approver_user_groups`
         (resolved via `group_member_check(group_id, user_id) -> bool`).
      3. Both lists empty AND the channel is a DM (id starts with "D") —
         only the installing user is in a DM with the bot, so the click
         author is implicit. This matches the config's DM-friendly default.

    Any other case rejects with an ephemeral "not authorized" and leaves
    the approval pending. Resolve is idempotent; re-resolving returns the
    prior record.

    `workspace` is stamped onto the log line so multi-workspace daemons
    produce clear audit trails.
    """
    if payload.get("type") != "block_actions":
        return ButtonClickResult(False, None, None, None, None)
    actions = payload.get("actions") or []
    if not actions:
        return ButtonClickResult(False, None, None, None, None)
    action = actions[0]
    parsed = parse_action_id(action.get("action_id", ""))
    if parsed is None:
        return ButtonClickResult(False, None, None, None, None)
    decision = parsed.decision
    modal_kind = parsed.modal_kind
    toggle_kind = parsed.toggle_kind
    modal_question_index = parsed.modal_question_index
    selected_option = parsed.selected_option
    selected_question = parsed.selected_question
    selected_suggestion = parsed.selected_suggestion

    approval_id = action.get("value") or ""
    user_id = (payload.get("user") or {}).get("id", "") or ""
    channel_id = (payload.get("channel") or {}).get("id")

    authorized, reject_reason = _authorize(
        slack_config, user_id, channel_id, group_member_check
    )
    if not authorized:
        if ephemeral_fn is not None and channel_id:
            try:
                ephemeral_fn(
                    channel=channel_id,
                    user=user_id,
                    text=":no_entry: You're not authorized to approve agent tool calls here.",
                )
            except Exception:
                logger.exception("failed to send ephemeral rejection to %s", user_id)
        return ButtonClickResult(True, None, reject_reason, approval_id, user_id)

    # Show more / Show less toggle: look up the persisted (preview, full)
    # payloads and chat.update the original message in place. The button's
    # `value` carries the message_id; `approval_id` is reused as the carrier
    # field name but it's actually a message_id here. No resolution / no
    # state change beyond the chat.update itself.
    if toggle_kind is not None:
        msg_id = action.get("value") or ""
        rec = expandable_messages.read(msg_id)
        if rec is None:
            logger.info(
                "workspace=%s payload=block_actions toggle=%s msg_id=%s rejected=unknown_toggle",
                workspace, toggle_kind, msg_id,
            )
            return ButtonClickResult(True, None, "unknown_toggle", msg_id, user_id)
        body = rec["full_body"] if toggle_kind == "expand" else rec["preview_body"]
        rec_channel = rec.get("channel") or channel_id
        rec_ts = rec.get("message_ts")
        if rec_channel and rec_ts and slack_config.bot_token:
            try:
                update_fn(slack_config.bot_token, rec_channel, rec_ts, body)
            except Exception:
                logger.exception("toggle chat.update failed for msg=%s", msg_id)
        logger.info(
            "workspace=%s payload=block_actions toggle=%s msg_id=%s user=%s",
            workspace, toggle_kind, msg_id, user_id,
        )
        return ButtonClickResult(True, None, None, msg_id, user_id)

    # Modal-trigger clicks: open the modal and return — the actual resolve
    # happens later when `view_submission` arrives. The `trigger_id` is
    # short-lived (3s per Slack docs), so views_open must run synchronously.
    if modal_kind is not None:
        # Defense-in-depth: when freeform_text="deny", the buttons aren't
        # rendered, but a stale message in scrollback could still surface
        # one. Refuse to open the modal and explain why.
        if slack_config.freeform_text != "allow":
            _post_freeform_disabled_hint(
                channel=channel_id, user=user_id, ephemeral_fn=ephemeral_fn,
            )
            return ButtonClickResult(
                True, None, "freeform_disabled", approval_id, user_id,
            )
        trigger_id = payload.get("trigger_id") or ""
        if not trigger_id or views_open_fn is None:
            # No way to open a modal — log and fall through; user can use
            # the one-tap buttons instead.
            logger.warning(
                "modal-trigger click but no trigger_id / views_open_fn (action=%s)",
                parsed.raw,
            )
            return ButtonClickResult(True, None, "no_trigger", approval_id, user_id)
        try:
            view = _build_modal_for(
                modal_kind, approval_id, modal_question_index, base_dir,
            )
        except Exception:
            logger.exception("failed to build modal for action=%s", parsed.raw)
            return ButtonClickResult(True, None, "modal_build_failed", approval_id, user_id)
        if view is None:
            # Approval missing — same UX as a stale option click.
            if ephemeral_fn is not None and channel_id:
                try:
                    ephemeral_fn(
                        channel=channel_id,
                        user=user_id,
                        text=":question: That approval is no longer pending (expired or already resolved).",
                    )
                except Exception:
                    logger.exception("failed to send stale-approval ephemeral to %s", user_id)
            return ButtonClickResult(True, None, "unknown_approval", approval_id, user_id)
        try:
            views_open_fn(trigger_id, view)
        except Exception:
            logger.exception("views_open failed for action=%s", parsed.raw)
            return ButtonClickResult(True, None, "views_open_failed", approval_id, user_id)
        return ButtonClickResult(True, None, None, approval_id, user_id)

    # If the hook already timed out and fell through to the TUI/Remote
    # prompt, a late Slack click is meaningless — Claude Code is no
    # longer waiting on us, and PostToolUse will deliver the actual
    # answer. Drop the click with an ephemeral hint instead of trying
    # to resolve.
    existing_record = pending_approvals.read(approval_id, base_dir=base_dir)
    if existing_record is not None and existing_record.get("decision") == "timed_out":
        if ephemeral_fn is not None and channel_id:
            try:
                ephemeral_fn(
                    channel=channel_id,
                    user=user_id,
                    text=":hourglass: Slack timed out for this prompt — please answer in your terminal or Claude Code Remote.",
                )
            except Exception:
                logger.exception("failed to send timed-out ephemeral to %s", user_id)
        return ButtonClickResult(True, None, "timed_out", approval_id, user_id)

    # Multi-question: an option click is a partial answer if other
    # questions still need answering. We record it (without resolving),
    # update the message to reflect progress, and only resolve once every
    # question has an entry. Single-question clicks (legacy), suggestion
    # clicks, and Approve/Deny clicks resolve immediately.
    rec = _record_or_resolve(
        approval_id,
        decision,
        user_id,
        selected_question,
        selected_option,
        selected_suggestion,
        base_dir,
        resolve_fn=resolve_fn,
    )
    if rec is None:
        if ephemeral_fn is not None and channel_id:
            try:
                ephemeral_fn(
                    channel=channel_id,
                    user=user_id,
                    text=":question: That approval is no longer pending (expired or already resolved).",
                )
            except Exception:
                logger.exception("failed to send stale-approval ephemeral to %s", user_id)
        return ButtonClickResult(True, None, "unknown_approval", approval_id, user_id)

    msg_channel = rec.get("channel")
    msg_ts = rec.get("message_ts")
    if msg_channel and msg_ts and slack_config.bot_token:
        try:
            event = _event_from_record(rec)
            is_resolved = rec.get("decision") is not None
            if is_resolved:
                selected_label = _selected_label_from_record(rec) if selected_option is not None else None
                selected_options = rec.get("selected_options") if rec.get("selected_options") else None
                freeform_answers = rec.get("freeform_answers") if rec.get("freeform_answers") else None
                # Suggestion-click: look up the chosen suggestion's
                # human label so the resolved message tells the user
                # what rule edit was applied.
                suggestion_label = None
                sugg_idx = rec.get("selected_suggestion_index")
                suggestions = rec.get("permission_suggestions")
                if (isinstance(sugg_idx, int) and isinstance(suggestions, list)
                        and 0 <= sugg_idx < len(suggestions)
                        and isinstance(suggestions[sugg_idx], dict)):
                    suggestion_label = _suggestion_label(suggestions[sugg_idx])
                body = build_resolved_message(
                    event, decision, f"<@{user_id}>",
                    selected_label=selected_label,
                    selected_options=selected_options,
                    freeform_answers=freeform_answers,
                    selected_suggestion_label=suggestion_label,
                )
            else:
                # Partial answer: re-render the approval message with
                # ✓ marks on the answered options. Buttons stay tappable
                # for unanswered questions.
                body = build_approval_message(
                    event, approval_id,
                    selected_options=rec.get("selected_options") or {},
                    freeform_answers=rec.get("freeform_answers") or {},
                    allow_freeform_text=slack_config.freeform_text == "allow",
                )
            update_fn(slack_config.bot_token, msg_channel, msg_ts, body)
        except Exception:
            logger.exception("failed to chat.update original approval message")

    final_decision = rec.get("decision")
    return ButtonClickResult(True, final_decision, None, approval_id, user_id)


def _build_modal_for(
    modal_kind: str,
    approval_id: str,
    question_index: int | None,
    base_dir: Path | None,
) -> dict | None:
    """Look up state from the pending record + return the modal view dict.

    Returns None when the approval is gone (the caller surfaces a stale-
    approval ephemeral). For custom_answer, also pre-fills the input with
    any previously-submitted text so re-opening the modal isn't a blank
    slate.
    """
    if modal_kind == "deny_reason":
        return build_deny_reason_modal(approval_id)
    if modal_kind == "custom_answer":
        rec = pending_approvals.read(approval_id, base_dir=base_dir)
        if rec is None:
            return None
        questions = _ask_user_question_questions(
            rec.get("tool_name"), rec.get("tool_input"),
        )
        q_idx = question_index or 0
        question_text = ""
        if questions and 0 <= q_idx < len(questions):
            question_text = questions[q_idx].get("question") or ""
        existing = rec.get("freeform_answers")
        initial = None
        if isinstance(existing, dict):
            val = existing.get(str(q_idx))
            if isinstance(val, str):
                initial = val
        return build_custom_answer_modal(
            approval_id, q_idx, question_text, initial_value=initial,
        )
    return None


def handle_view_submission(
    payload: dict[str, Any],
    slack_config: SlackConfig,
    *,
    workspace: str = "default",
    resolve_fn: Callable[..., dict | None] = pending_approvals.resolve,
    record_partial_fn: Callable[..., dict | None] = pending_approvals.record_partial_answer,
    update_fn: Callable[..., None] = update_message,
    ephemeral_fn: Callable[..., None] | None = None,
    base_dir: Path | None = None,
) -> ButtonClickResult:
    """Act on a Slack `view_submission` payload — the user submitted a
    custom-answer or deny-reason modal.

    Auth: same allowlist semantics as block_actions, but the channel context
    isn't on the payload (modals submit out-of-band). We require the user
    to be in `approver_user_ids` or a configured group; the DM-only
    fallback doesn't apply here. This is a tighter rule than block_actions
    (no channel-id signal to validate) — but in practice a user only sees
    the modal trigger if they were already authorized to click it.
    """
    if payload.get("type") != "view_submission":
        return ButtonClickResult(False, None, None, None, None)
    view = payload.get("view") or {}
    if not isinstance(view, dict):
        return ButtonClickResult(False, None, None, None, None)
    callback_id = view.get("callback_id", "")
    user_id = (payload.get("user") or {}).get("id", "") or ""
    text = extract_modal_text(view)
    if not text:
        # Slack's `min_length: 1` should prevent this; defensive no-op.
        return ButtonClickResult(True, None, "empty_text", None, user_id)

    metadata_raw = view.get("private_metadata") or "{}"
    try:
        import json as _json
        metadata = _json.loads(metadata_raw)
    except (ValueError, TypeError):
        return ButtonClickResult(True, None, "bad_metadata", None, user_id)
    if not isinstance(metadata, dict):
        return ButtonClickResult(True, None, "bad_metadata", None, user_id)
    approval_id = metadata.get("approval_id") or ""
    if not isinstance(approval_id, str) or not approval_id:
        return ButtonClickResult(True, None, "bad_metadata", None, user_id)

    # Defense-in-depth: a stale modal could submit after freeform_text was
    # flipped to "deny" — refuse the submission and post an ephemeral hint
    # to the originating channel. The pending approval keeps waiting for a
    # button click.
    if slack_config.freeform_text != "allow" and callback_id in (
        MODAL_CALLBACK_CUSTOM_ANSWER, MODAL_CALLBACK_DENY_REASON,
    ):
        existing = pending_approvals.read(approval_id, base_dir=base_dir)
        channel = (existing or {}).get("channel") if isinstance(existing, dict) else None
        _post_freeform_disabled_hint(
            channel=channel, user=user_id, ephemeral_fn=ephemeral_fn,
        )
        return ButtonClickResult(True, None, "freeform_disabled", approval_id, user_id)

    # Same timed-out guard as handle_block_actions: if the hook already
    # fell through, Claude Code isn't waiting for our decision anymore.
    existing_record = pending_approvals.read(approval_id, base_dir=base_dir)
    if existing_record is not None and existing_record.get("decision") == "timed_out":
        return ButtonClickResult(True, None, "timed_out", approval_id, user_id)

    if callback_id == MODAL_CALLBACK_DENY_REASON:
        rec = resolve_fn(
            approval_id, "deny", actor=user_id,
            deny_reason=text, base_dir=base_dir,
        )
    elif callback_id == MODAL_CALLBACK_CUSTOM_ANSWER:
        question_index = metadata.get("question_index")
        if not isinstance(question_index, int):
            return ButtonClickResult(True, None, "bad_metadata", approval_id, user_id)
        rec = _record_or_resolve_text(
            approval_id, user_id, question_index, text, base_dir,
            resolve_fn=resolve_fn, record_partial_fn=record_partial_fn,
        )
    else:
        return ButtonClickResult(False, None, None, approval_id, user_id)

    if rec is None:
        return ButtonClickResult(True, None, "unknown_approval", approval_id, user_id)

    msg_channel = rec.get("channel")
    msg_ts = rec.get("message_ts")
    if msg_channel and msg_ts and slack_config.bot_token:
        try:
            event = _event_from_record(rec)
            decision = rec.get("decision")
            if decision is not None:
                # Final resolve — render the resolved message.
                selected_options = rec.get("selected_options") or None
                freeform_answers = rec.get("freeform_answers") or None
                selected_label = _selected_label_from_record(rec)
                deny_reason = rec.get("deny_reason") if decision == "deny" else None
                # Suggestion clicks aren't reachable from a modal; pass None.
                body = build_resolved_message(
                    event, decision, f"<@{user_id}>",
                    selected_label=selected_label,
                    selected_options=selected_options,
                    freeform_answers=freeform_answers,
                    selected_suggestion_label=None,
                    deny_reason=deny_reason,
                )
            else:
                # Partial answer for multi-Q custom answer — re-render with ✓.
                body = build_approval_message(
                    event, approval_id,
                    selected_options=rec.get("selected_options") or {},
                    freeform_answers=rec.get("freeform_answers") or {},
                    allow_freeform_text=slack_config.freeform_text == "allow",
                )
            update_fn(slack_config.bot_token, msg_channel, msg_ts, body)
        except Exception:
            logger.exception("failed to chat.update after view_submission")

    return ButtonClickResult(True, rec.get("decision"), None, approval_id, user_id)


def _record_or_resolve_text(
    approval_id: str,
    user_id: str,
    question_index: int,
    text: str,
    base_dir: Path | None,
    *,
    resolve_fn: Callable[..., dict | None],
    record_partial_fn: Callable[..., dict | None],
) -> dict | None:
    """Custom-answer modal submission: record the typed text for one
    question. Resolve immediately if there's only one question OR if every
    question now has an answer (option click or freeform text).
    """
    existing = pending_approvals.read(approval_id, base_dir=base_dir)
    if existing is None:
        return None
    questions = _ask_user_question_questions(
        existing.get("tool_name"), existing.get("tool_input"),
    )
    if not questions or len(questions) <= 1:
        # Single-question (or malformed): resolve immediately with the
        # freeform text as the sole answer.
        return resolve_fn(
            approval_id, "allow", actor=user_id,
            freeform_answers={str(question_index): text},
            base_dir=base_dir,
        )
    rec = record_partial_fn(
        approval_id, question_index,
        text=text, actor=user_id, base_dir=base_dir,
    )
    if rec is None:
        return None
    selected_options = rec.get("selected_options") or {}
    freeform_answers = rec.get("freeform_answers") or {}
    answerable_indices = {
        str(i) for i, q in enumerate(questions) if q.get("multiSelect") is not True
    }
    answered = set(selected_options.keys()) | set(freeform_answers.keys())
    if answerable_indices.issubset(answered):
        return resolve_fn(
            approval_id, "allow", actor=user_id,
            selected_options=selected_options,
            freeform_answers=freeform_answers,
            base_dir=base_dir,
        )
    return rec


def _record_or_resolve(
    approval_id: str,
    decision: str,
    user_id: str,
    selected_question: int | None,
    selected_option: int | None,
    selected_suggestion: int | None,
    base_dir: Path | None,
    *,
    resolve_fn: Callable[..., dict | None],
) -> dict | None:
    """For Approve/Deny, suggestion, and single-question (legacy) clicks,
    resolve immediately. For multi-question option clicks, record the
    partial answer and resolve only when every question has an entry —
    otherwise return the partially-answered record so the daemon updates
    the message without unblocking the hook.

    Returns the (partial or final) record, or None if the approval doesn't
    exist.
    """
    # Approve / Deny / Suggestion short-circuit multi-question logic.
    if selected_question is None:
        return resolve_fn(
            approval_id, decision, actor=user_id,
            selected_option=selected_option,
            selected_suggestion=selected_suggestion,
            base_dir=base_dir,
        )

    # Read the existing record to know how many questions there are.
    existing = pending_approvals.read(approval_id, base_dir=base_dir)
    if existing is None:
        return None
    questions = _ask_user_question_questions(
        existing.get("tool_name"), existing.get("tool_input"),
    )
    # Buttons-rendered single-question case OR fewer than 2 questions:
    # resolve immediately with the legacy single-index field for back-compat.
    if not questions or len(questions) <= 1:
        return resolve_fn(
            approval_id, decision, actor=user_id,
            selected_option=selected_option, base_dir=base_dir,
        )

    # Multi-question: record this answer.
    rec = pending_approvals.record_partial_answer(
        approval_id, selected_question,
        option_index=selected_option or 0,
        actor=user_id, base_dir=base_dir,
    )
    if rec is None:
        return None
    selected_options = rec.get("selected_options") or {}
    freeform_answers = rec.get("freeform_answers") or {}
    # Count answerable (non-multiSelect) questions — only those we render
    # buttons for. multiSelect questions are surfaced as text-only and
    # don't gate resolution from Slack.
    answerable_indices = {
        str(i) for i, q in enumerate(questions) if q.get("multiSelect") is not True
    }
    answered = set(selected_options.keys()) | set(freeform_answers.keys())
    if answerable_indices.issubset(answered):
        # All button-renderable questions answered → finalize.
        return resolve_fn(
            approval_id, decision, actor=user_id,
            selected_options=selected_options,
            freeform_answers=freeform_answers,
            base_dir=base_dir,
        )
    return rec


def _authorize(
    slack_config: SlackConfig,
    user_id: str,
    channel_id: str | None,
    group_member_check: Callable[[str, str], bool] | None,
) -> tuple[bool, str | None]:
    """Return (is_allowed, reject_reason).

    Reject reasons: `"not_authorized"` (user exists but isn't allowlisted)
    or `"no_allowlist_non_dm"` (empty allowlist + click from a shared
    channel — the DM fallback only applies to actual DMs).
    """
    if user_id and user_id in slack_config.approver_user_ids:
        return True, None
    if slack_config.approver_user_groups and group_member_check and user_id:
        for group_id in slack_config.approver_user_groups:
            try:
                if group_member_check(group_id, user_id):
                    return True, None
            except Exception:
                # Group resolution failure doesn't grant access; fall through
                # to other checks. Log so operators can see API failures.
                logger.exception("group_member_check raised for group=%s", group_id)
    if not slack_config.approver_user_ids and not slack_config.approver_user_groups:
        if channel_id and channel_id.startswith(_DM_CHANNEL_PREFIX):
            return True, None
        return False, "no_allowlist_non_dm"
    return False, "not_authorized"


def _event_from_record(rec: dict) -> Event:
    # Reconstruct just enough of the original Event for `build_resolved_message`
    # / `build_approval_message`. cwd isn't persisted yet; a `.` here only
    # affects the footer folder name.
    suggestions = rec.get("permission_suggestions")
    return Event(
        agent=rec.get("agent") or "claude-code",
        kind="permission",
        message="",
        cwd=Path("."),
        session_id=rec.get("session_id"),
        tool_name=rec.get("tool_name"),
        tool_input=rec.get("tool_input") if isinstance(rec.get("tool_input"), dict) else None,
        permission_suggestions=(
            tuple(s for s in suggestions if isinstance(s, dict))
            if isinstance(suggestions, list) and suggestions else None
        ),
    )


def _interactive_workspaces(config: Config) -> list[tuple[str, SlackConfig]]:
    """Return (name, cfg) pairs for workspaces with actionable_approvals=true
    and both tokens resolved. Back-compat: if `slack_workspaces` is empty but
    the legacy `config.slack` has actionable_approvals, treat it as "default"."""
    out: list[tuple[str, SlackConfig]] = []
    for name, ws in config.slack_workspaces.items():
        if ws.actionable_approvals and ws.bot_token and ws.app_token:
            out.append((name, ws))
    if not out and config.slack.actionable_approvals and config.slack.bot_token and config.slack.app_token:
        out.append(("default", config.slack))
    return out


def run_daemon(config: Config, *, stop_event: threading.Event | None = None) -> None:
    """Run one Socket Mode listener per Slack workspace with actionable_approvals.

    Each workspace runs in its own thread with its own `WebClient` and
    `SocketModeClient`, so clicks always route back through the bot that
    posted the message. Threads share a single `stop_event`; cleanup
    disconnects every client before returning.

    Lazy-imports `slack_sdk` so the base package stays dep-free. Raises
    `RuntimeError` if no workspace is configured for actionable approvals.
    """
    try:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse
    except ImportError as e:
        raise RuntimeError(
            "slack-sdk is required for the daemon. Install with: "
            "`uv tool install 'coding-agent-notifier[slack-bot]' --reinstall` "
            "(or `uv pip install slack-sdk` into the existing tool env)."
        ) from e

    workspaces = _interactive_workspaces(config)
    if not workspaces:
        raise RuntimeError(
            "Slack daemon requires at least one workspace with actionable_approvals=true "
            "plus bot_token (xoxb-*) and app_token (xapp-*)."
        )

    # Opportunistic GC of stale expandable-message records on startup. Cheap
    # (handful of file stats), and the daemon restart cadence is the natural
    # place to reap — no separate cron / launchd needed.
    try:
        removed = expandable_messages.gc_stale()
        if removed:
            logger.info("expandable_messages gc removed %d stale record(s)", removed)
    except Exception:
        logger.exception("expandable_messages gc_stale failed on daemon startup")

    stop = stop_event or threading.Event()
    clients: list[Any] = []
    threads: list[threading.Thread] = []

    for name, ws_cfg in workspaces:
        client = _start_workspace_listener(
            name, ws_cfg, WebClient, SocketModeClient, SocketModeRequest,
            SocketModeResponse,
        )
        clients.append(client)

    logger.info(
        "agent-notify daemon connected to Slack Socket Mode for workspaces: %s",
        ", ".join(name for name, _ in workspaces),
    )

    try:
        while not stop.is_set():
            stop.wait(timeout=60.0)
    finally:
        for client in clients:
            try:
                client.disconnect()
            except Exception:
                pass
        for t in threads:
            t.join(timeout=5.0)


def _start_workspace_listener(
    workspace_name: str,
    slack_config: SlackConfig,
    WebClient, SocketModeClient, SocketModeRequest, SocketModeResponse,
):
    """Wire up a single workspace's Socket Mode client + listener. Returns
    the connected SocketModeClient so the caller can disconnect on shutdown.
    """
    web = WebClient(token=slack_config.bot_token)
    group_check = _make_group_member_check(web)

    def _ephemeral(channel: str, user: str, text: str) -> None:
        web.chat_postEphemeral(channel=channel, user=user, text=text)

    def _update(bot_token: str, channel: str, ts: str, body: dict) -> None:
        # bot_token already baked into `web`; ignore the arg.
        web.chat_update(channel=channel, ts=ts, **body)

    def _views_open(trigger_id: str, view: dict) -> None:
        web.views_open(trigger_id=trigger_id, view=view)

    def listener(client, req) -> None:
        # ACK immediately — Slack retries anything unacked within ~3s.
        try:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:
            logger.exception("failed to ack envelope %s", req.envelope_id)

        if req.type != "interactive":
            return
        payload = req.payload if isinstance(req.payload, dict) else None
        if payload is None:
            return
        payload_type = payload.get("type")
        try:
            if payload_type == "view_submission":
                result = handle_view_submission(
                    payload,
                    slack_config,
                    workspace=workspace_name,
                    update_fn=_update,
                    ephemeral_fn=_ephemeral,
                )
            else:
                result = handle_block_actions(
                    payload,
                    slack_config,
                    workspace=workspace_name,
                    ephemeral_fn=_ephemeral,
                    update_fn=_update,
                    views_open_fn=_views_open,
                    group_member_check=group_check,
                )
        except Exception:
            logger.exception("handler raised while processing Slack interaction")
            return
        if result.handled:
            logger.info(
                "workspace=%s payload=%s approval=%s decision=%s user=%s rejected=%s",
                workspace_name,
                payload_type,
                result.approval_id,
                result.decision,
                result.user_id,
                result.rejected_reason,
            )

    client = SocketModeClient(app_token=slack_config.app_token, web_client=web)
    client.socket_mode_request_listeners.append(listener)
    client.connect()
    return client


def _make_group_member_check(web_client: Any) -> Callable[[str, str], bool]:
    """Return a (group_id, user_id) → bool checker that caches results.

    Calls `usergroups.users.list` once per group per TTL window (5 minutes
    by default) so a burst of button clicks doesn't hammer the API. A
    lookup failure raises to the caller, which in `_authorize` is caught
    and logged; failures never grant access.
    """
    cache: dict[str, tuple[float, set[str]]] = {}
    lock = threading.Lock()

    def _resolve(group_id: str) -> set[str]:
        now = time.monotonic()
        with lock:
            hit = cache.get(group_id)
            if hit and (now - hit[0]) < _GROUP_CACHE_TTL_SECONDS:
                return hit[1]
        response = web_client.usergroups_users_list(usergroup=group_id)
        # slack_sdk returns a SlackResponse-like object with .data (dict)
        data = getattr(response, "data", response)
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(
                f"usergroups.users.list failed for {group_id}: "
                f"{data.get('error') if isinstance(data, dict) else data!r}"
            )
        members = set(data.get("users") or [])
        with lock:
            cache[group_id] = (now, members)
        return members

    def _check(group_id: str, user_id: str) -> bool:
        return user_id in _resolve(group_id)

    return _check


_GROUP_CACHE_TTL_SECONDS = 300.0  # 5 minutes
