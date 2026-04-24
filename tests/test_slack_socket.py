from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coding_agent_notifier import pending_approvals as pa
from coding_agent_notifier import slack_socket
from coding_agent_notifier.config import SlackConfig


def _slack_config(**kw) -> SlackConfig:
    base = dict(
        enabled=True,
        bot_token="xoxb-test",
        app_token="xapp-test",
        channel="C1",
        interactive=True,
        actionable_approvals=True,
        approver_user_ids=("U_OK",),
        approval_timeout_seconds=60.0,
    )
    base.update(kw)
    return SlackConfig(**base)


def _payload(action_id: str, *, value: str = "appr-1", user_id: str = "U_OK",
             channel_id: str = "C1") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user_id},
        "channel": {"id": channel_id},
        "actions": [{"action_id": action_id, "value": value}],
    }


def test_non_interactive_payload_not_handled():
    res = slack_socket.handle_block_actions({"type": "view_submission"}, _slack_config())
    assert res.handled is False


def test_unknown_action_id_not_handled():
    payload = _payload("some_other_button")
    res = slack_socket.handle_block_actions(payload, _slack_config())
    assert res.handled is False


def test_approve_resolves_pending(tmp_path: Path):
    pa.create("appr-1", agent="claude-code", session_id="s", tool_name="Bash",
              base_dir=tmp_path)
    updates: list[dict] = []

    def _update(bot_token, channel, ts, body):
        updates.append({"channel": channel, "ts": ts, "body": body})

    pa.set_message_ref("appr-1", "C1", "1.0", base_dir=tmp_path)
    res = slack_socket.handle_block_actions(
        _payload("agent_notify_approve"),
        _slack_config(),
        update_fn=_update,
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.decision == "allow"
    assert res.rejected_reason is None
    rec = pa.read("appr-1", base_dir=tmp_path)
    assert rec["decision"] == "allow"
    assert rec["actor"] == "U_OK"
    # chat.update called to edit the original message in place.
    assert len(updates) == 1
    assert updates[0]["channel"] == "C1"
    assert updates[0]["ts"] == "1.0"


def test_deny_resolves_pending(tmp_path: Path):
    pa.create("appr-1", agent="claude-code", session_id="s", tool_name="Bash",
              base_dir=tmp_path)
    pa.set_message_ref("appr-1", "C1", "1.0", base_dir=tmp_path)
    res = slack_socket.handle_block_actions(
        _payload("agent_notify_deny"),
        _slack_config(),
        update_fn=lambda *a, **kw: None,
        base_dir=tmp_path,
    )
    assert res.decision == "deny"
    rec = pa.read("appr-1", base_dir=tmp_path)
    assert rec["decision"] == "deny"


def test_non_allowlisted_user_rejected(tmp_path: Path):
    pa.create("appr-1", agent="claude-code", session_id="s", tool_name="Bash",
              base_dir=tmp_path)
    ephemerals: list[dict] = []

    def _ephem(**kw):
        ephemerals.append(kw)

    res = slack_socket.handle_block_actions(
        _payload("agent_notify_approve", user_id="U_ATTACKER"),
        _slack_config(approver_user_ids=("U_OK",)),
        ephemeral_fn=_ephem,
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.decision is None
    assert res.rejected_reason == "not_authorized"
    # Original approval still pending — attacker didn't resolve it.
    rec = pa.read("appr-1", base_dir=tmp_path)
    assert rec["decision"] is None
    # User got an ephemeral "not authorized".
    assert len(ephemerals) == 1
    assert ephemerals[0]["user"] == "U_ATTACKER"
    assert "not authorized" in ephemerals[0]["text"].lower()


def test_empty_allowlist_allows_anyone(tmp_path: Path):
    """Defaulting to wide-open is a conscious choice (DM mode assumed);
    config parser nudges users to fill this in."""
    pa.create("appr-1", agent="claude-code", session_id="s", tool_name="Bash",
              base_dir=tmp_path)
    res = slack_socket.handle_block_actions(
        _payload("agent_notify_approve", user_id="U_RANDO"),
        _slack_config(approver_user_ids=()),
        update_fn=lambda *a, **kw: None,
        base_dir=tmp_path,
    )
    assert res.decision == "allow"


def test_unknown_approval_id_sends_stale_ephemeral(tmp_path: Path):
    ephemerals: list[dict] = []

    def _ephem(**kw):
        ephemerals.append(kw)

    res = slack_socket.handle_block_actions(
        _payload("agent_notify_approve", value="ghost-id"),
        _slack_config(),
        ephemeral_fn=_ephem,
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.rejected_reason == "unknown_approval"
    assert len(ephemerals) == 1
    assert "no longer pending" in ephemerals[0]["text"]


def test_chat_update_failure_does_not_fail_resolve(tmp_path: Path):
    """If Slack rejects the chat.update, the decision still stands —
    swallow the update error so the agent hook can still proceed."""
    pa.create("appr-1", agent="claude-code", session_id="s", tool_name="Bash",
              base_dir=tmp_path)
    pa.set_message_ref("appr-1", "C1", "1.0", base_dir=tmp_path)

    def _update(*a, **kw):
        raise RuntimeError("slack is down")

    res = slack_socket.handle_block_actions(
        _payload("agent_notify_approve"),
        _slack_config(),
        update_fn=_update,
        base_dir=tmp_path,
    )
    assert res.decision == "allow"
    rec = pa.read("appr-1", base_dir=tmp_path)
    assert rec["decision"] == "allow"


def test_empty_actions_array_not_handled():
    payload = {"type": "block_actions", "user": {"id": "U"}, "actions": []}
    res = slack_socket.handle_block_actions(payload, _slack_config())
    assert res.handled is False
