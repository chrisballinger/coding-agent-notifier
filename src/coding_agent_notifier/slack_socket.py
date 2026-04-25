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

from . import pending_approvals
from .config import Config, SlackConfig
from .event import Event
from .sinks.slack import (
    APPROVE_ACTION_ID,
    DENY_ACTION_ID,
    OPTION_ACTION_ID_PREFIX,
    _ask_user_question_questions,
    _selected_label_from_record,
    build_approval_message,
    build_resolved_message,
    update_message,
)

logger = logging.getLogger(__name__)

# Slack DM channel IDs start with "D" (e.g. "D12345ABC"). Used in the
# empty-allowlist fallback to verify the click actually came from a DM
# rather than a shared channel — defense-in-depth against misconfigured
# channels or weird Slack routing.
_DM_CHANNEL_PREFIX = "D"


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
    action_id = action.get("action_id", "")
    # Single-question (legacy) click: agent_notify_option_<o>
    # Multi-question click: agent_notify_option_<q>_<o>
    selected_option: int | None = None
    selected_question: int | None = None
    if action_id == APPROVE_ACTION_ID:
        decision = "allow"
    elif action_id == DENY_ACTION_ID:
        decision = "deny"
    elif action_id.startswith(OPTION_ACTION_ID_PREFIX):
        suffix = action_id[len(OPTION_ACTION_ID_PREFIX):]
        parts = suffix.split("_")
        try:
            if len(parts) == 1:
                # Legacy single-question encoding — treat as question 0.
                selected_question = 0
                selected_option = int(parts[0])
            elif len(parts) == 2:
                selected_question = int(parts[0])
                selected_option = int(parts[1])
            else:
                return ButtonClickResult(False, None, None, None, None)
        except ValueError:
            return ButtonClickResult(False, None, None, None, None)
        decision = "allow"
    else:
        return ButtonClickResult(False, None, None, None, None)

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

    # Multi-question: an option click is a partial answer if other
    # questions still need answering. We record it (without resolving),
    # update the message to reflect progress, and only resolve once every
    # question has an entry. Single-question clicks (legacy) and Approve/
    # Deny clicks resolve immediately.
    rec = _record_or_resolve(
        approval_id,
        decision,
        user_id,
        selected_question,
        selected_option,
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
                body = build_resolved_message(
                    event, decision, f"<@{user_id}>",
                    selected_label=selected_label,
                    selected_options=selected_options,
                )
            else:
                # Partial answer: re-render the approval message with
                # ✓ marks on the answered options. Buttons stay tappable
                # for unanswered questions.
                body = build_approval_message(
                    event, approval_id,
                    selected_options=rec.get("selected_options") or {},
                )
            update_fn(slack_config.bot_token, msg_channel, msg_ts, body)
        except Exception:
            logger.exception("failed to chat.update original approval message")

    final_decision = rec.get("decision")
    return ButtonClickResult(True, final_decision, None, approval_id, user_id)


def _record_or_resolve(
    approval_id: str,
    decision: str,
    user_id: str,
    selected_question: int | None,
    selected_option: int | None,
    base_dir: Path | None,
    *,
    resolve_fn: Callable[..., dict | None],
) -> dict | None:
    """For Approve/Deny and single-question (legacy) clicks, resolve
    immediately. For multi-question option clicks, record the partial
    answer and resolve only when every question has an entry — otherwise
    return the partially-answered record so the daemon updates the
    message without unblocking the hook.

    Returns the (partial or final) record, or None if the approval doesn't
    exist.
    """
    # Approve / Deny short-circuits multi-question logic.
    if selected_question is None:
        return resolve_fn(
            approval_id, decision, actor=user_id,
            selected_option=selected_option, base_dir=base_dir,
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
        approval_id, selected_question, selected_option or 0,
        actor=user_id, base_dir=base_dir,
    )
    if rec is None:
        return None
    selected_options = rec.get("selected_options") or {}
    # Count answerable (non-multiSelect) questions — only those we render
    # buttons for. multiSelect questions are surfaced as text-only and
    # don't gate resolution from Slack.
    answerable_indices = {
        str(i) for i, q in enumerate(questions) if q.get("multiSelect") is not True
    }
    if answerable_indices.issubset(selected_options.keys()):
        # All button-renderable questions answered → finalize.
        return resolve_fn(
            approval_id, decision, actor=user_id,
            selected_options=selected_options, base_dir=base_dir,
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
    # Reconstruct just enough of the original Event for `build_resolved_message`.
    # cwd isn't persisted yet; a `.` here only affects the footer folder name.
    return Event(
        agent=rec.get("agent") or "claude-code",
        kind="permission",
        message="",
        cwd=Path("."),
        session_id=rec.get("session_id"),
        tool_name=rec.get("tool_name"),
        tool_input=rec.get("tool_input") if isinstance(rec.get("tool_input"), dict) else None,
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
        try:
            result = handle_block_actions(
                payload,
                slack_config,
                workspace=workspace_name,
                ephemeral_fn=_ephemeral,
                update_fn=_update,
                group_member_check=group_check,
            )
        except Exception:
            logger.exception("handler raised while processing Slack interaction")
            return
        if result.handled:
            logger.info(
                "workspace=%s approval=%s decision=%s user=%s rejected=%s",
                workspace_name,
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
