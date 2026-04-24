"""Slack Socket Mode daemon + button-click handler.

The daemon opens a WebSocket from this machine out to Slack (no inbound
port). Interactive payloads — approve/deny button clicks — arrive over the
WS. The handler resolves the matching `PendingApproval`, which unblocks
the `PreToolUse` hook that was waiting on the FIFO, and edits the original
message in place to show the outcome.

`handle_block_actions` is pure-ish (side effects injected) so tests can
exercise the decision logic without `slack_sdk` or a real WS. `run_daemon`
is the thin wrapper that imports `slack_sdk` and loops.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import pending_approvals
from .config import Config, SlackConfig
from .event import Event
from .sinks.slack import (
    APPROVE_ACTION_ID,
    DENY_ACTION_ID,
    build_resolved_message,
    update_message,
)

logger = logging.getLogger(__name__)


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
    resolve_fn: Callable[..., dict | None] = pending_approvals.resolve,
    ephemeral_fn: Callable[..., None] | None = None,
    update_fn: Callable[..., None] = update_message,
    base_dir: Path | None = None,
) -> ButtonClickResult:
    """Act on a Slack `block_actions` payload.

    Contract:
      - Only acts on our own `action_id`s — foreign interactions return
        `handled=False` so the daemon can log and move on.
      - User allowlist enforced via `slack_config.approver_user_ids`.
      - Empty allowlist = allow any user in the channel (caller's choice;
        config validation nudges users toward setting it).
      - Resolve is idempotent; re-resolving returns the prior record.
    """
    if payload.get("type") != "block_actions":
        return ButtonClickResult(False, None, None, None, None)
    actions = payload.get("actions") or []
    if not actions:
        return ButtonClickResult(False, None, None, None, None)
    action = actions[0]
    action_id = action.get("action_id", "")
    if action_id not in (APPROVE_ACTION_ID, DENY_ACTION_ID):
        return ButtonClickResult(False, None, None, None, None)

    approval_id = action.get("value") or ""
    user_id = (payload.get("user") or {}).get("id", "") or ""
    channel_id = (payload.get("channel") or {}).get("id")
    decision = "allow" if action_id == APPROVE_ACTION_ID else "deny"

    allowed = slack_config.approver_user_ids
    if allowed and user_id not in allowed:
        if ephemeral_fn is not None and channel_id:
            try:
                ephemeral_fn(
                    channel=channel_id,
                    user=user_id,
                    text=":no_entry: You're not authorized to approve agent tool calls here.",
                )
            except Exception:
                logger.exception("failed to send ephemeral rejection to %s", user_id)
        return ButtonClickResult(True, None, "not_authorized", approval_id, user_id)

    rec = resolve_fn(approval_id, decision, actor=user_id, base_dir=base_dir)
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
            body = build_resolved_message(event, decision, f"<@{user_id}>")
            update_fn(slack_config.bot_token, msg_channel, msg_ts, body)
        except Exception:
            logger.exception("failed to chat.update original approval message")

    return ButtonClickResult(True, decision, None, approval_id, user_id)


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


def run_daemon(config: Config, *, stop_event: threading.Event | None = None) -> None:
    """Run the Socket Mode listener until `stop_event` fires (or forever).

    Lazy-imports `slack_sdk` so the base package stays dep-free. Raises
    `RuntimeError` if the Slack bot / app tokens aren't configured.
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

    slack_config = config.slack
    if not slack_config.bot_token:
        raise RuntimeError("Slack daemon requires sinks.slack.bot_token")
    if not slack_config.app_token:
        raise RuntimeError(
            "Slack daemon requires sinks.slack.app_token (xapp-* Socket Mode token)"
        )

    web = WebClient(token=slack_config.bot_token)

    def _ephemeral(channel: str, user: str, text: str) -> None:
        web.chat_postEphemeral(channel=channel, user=user, text=text)

    def _update(bot_token: str, channel: str, ts: str, body: dict) -> None:
        # bot_token already baked into `web`; ignore the arg.
        web.chat_update(channel=channel, ts=ts, **body)

    def listener(client: "SocketModeClient", req: "SocketModeRequest") -> None:
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
                ephemeral_fn=_ephemeral,
                update_fn=_update,
            )
        except Exception:
            logger.exception("handler raised while processing Slack interaction")
            return
        if result.handled:
            logger.info(
                "approval %s decision=%s user=%s rejected=%s",
                result.approval_id,
                result.decision,
                result.user_id,
                result.rejected_reason,
            )

    client = SocketModeClient(app_token=slack_config.app_token, web_client=web)
    client.socket_mode_request_listeners.append(listener)
    client.connect()
    logger.info("agent-notify daemon connected to Slack Socket Mode")

    stop = stop_event or threading.Event()
    try:
        # `Event.wait` is the idiomatic "block until signaled" in stdlib.
        # Timeout makes the loop responsive to OS signals if we add handling
        # later (for clean launchd shutdown).
        while not stop.is_set():
            stop.wait(timeout=60.0)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
