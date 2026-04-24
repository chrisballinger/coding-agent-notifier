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


def test_empty_allowlist_allows_dm_channel(tmp_path: Path):
    """Empty allowlist is secure-by-fallback in a DM: Slack DM channel IDs
    start with 'D', and only the installing user is in a DM with the bot,
    so the click author is implicit. Matches the config's DM-friendly
    default (`channel = "@me"` with no approver_user_ids)."""
    pa.create("appr-1", agent="claude-code", session_id="s", tool_name="Bash",
              base_dir=tmp_path)
    pa.set_message_ref("appr-1", "D_BOT_DM", "1.0", base_dir=tmp_path)
    res = slack_socket.handle_block_actions(
        _payload("agent_notify_approve", user_id="U_SELF", channel_id="D_BOT_DM"),
        _slack_config(approver_user_ids=()),
        update_fn=lambda *a, **kw: None,
        base_dir=tmp_path,
    )
    assert res.decision == "allow"


def test_empty_allowlist_rejects_shared_channel(tmp_path: Path):
    """The DM fallback does NOT extend to shared channels — if the config
    somehow made it through with empty allowlist on a C-prefixed channel,
    the runtime check rejects."""
    pa.create("appr-1", agent="claude-code", session_id="s", tool_name="Bash",
              base_dir=tmp_path)
    ephemerals: list[dict] = []
    res = slack_socket.handle_block_actions(
        _payload("agent_notify_approve", user_id="U_RANDO", channel_id="C_SHARED"),
        _slack_config(approver_user_ids=()),
        ephemeral_fn=lambda **kw: ephemerals.append(kw),
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.decision is None
    assert res.rejected_reason == "no_allowlist_non_dm"
    rec = pa.read("appr-1", base_dir=tmp_path)
    assert rec["decision"] is None
    assert len(ephemerals) == 1


def test_usergroup_membership_authorizes(tmp_path: Path):
    """A clicker who isn't in `approver_user_ids` but IS in one of the
    approver_user_groups gets through."""
    pa.create("appr-1", agent="claude-code", session_id="s", tool_name="Bash",
              base_dir=tmp_path)
    pa.set_message_ref("appr-1", "C_TEAM", "1.0", base_dir=tmp_path)
    calls: list[tuple[str, str]] = []

    def _check(group_id: str, user_id: str) -> bool:
        calls.append((group_id, user_id))
        return group_id == "S_ONCALL" and user_id == "U_ONCALL"

    res = slack_socket.handle_block_actions(
        _payload("agent_notify_approve", user_id="U_ONCALL", channel_id="C_TEAM"),
        _slack_config(approver_user_ids=(), approver_user_groups=("S_ONCALL",)),
        update_fn=lambda *a, **kw: None,
        group_member_check=_check,
        base_dir=tmp_path,
    )
    assert res.decision == "allow"
    assert calls == [("S_ONCALL", "U_ONCALL")]


def test_usergroup_membership_rejects_non_member(tmp_path: Path):
    pa.create("appr-1", agent="claude-code", session_id="s", tool_name="Bash",
              base_dir=tmp_path)
    res = slack_socket.handle_block_actions(
        _payload("agent_notify_approve", user_id="U_RANDO", channel_id="C_TEAM"),
        _slack_config(approver_user_ids=(), approver_user_groups=("S_ONCALL",)),
        ephemeral_fn=lambda **kw: None,
        group_member_check=lambda g, u: False,
        base_dir=tmp_path,
    )
    assert res.decision is None
    assert res.rejected_reason == "not_authorized"


def test_usergroup_resolver_failure_does_not_grant_access(tmp_path: Path):
    """If the Slack API call to resolve group membership blows up, we fall
    through to the next check (in this case, no other allowlist → reject).
    Never grant access on resolver failure."""
    pa.create("appr-1", agent="claude-code", session_id="s", tool_name="Bash",
              base_dir=tmp_path)

    def _broken(_g, _u):
        raise RuntimeError("Slack API returned 429")

    res = slack_socket.handle_block_actions(
        _payload("agent_notify_approve", user_id="U_RANDO", channel_id="C_TEAM"),
        _slack_config(approver_user_ids=(), approver_user_groups=("S_ONCALL",)),
        ephemeral_fn=lambda **kw: None,
        group_member_check=_broken,
        base_dir=tmp_path,
    )
    assert res.decision is None
    assert res.rejected_reason == "not_authorized"


def test_explicit_user_id_wins_over_group_check(tmp_path: Path):
    """If user_id is in approver_user_ids, we short-circuit and never call
    the group resolver — cheap, and avoids unnecessary API hits."""
    pa.create("appr-1", agent="claude-code", session_id="s", tool_name="Bash",
              base_dir=tmp_path)
    pa.set_message_ref("appr-1", "C_TEAM", "1.0", base_dir=tmp_path)

    def _should_not_be_called(_g, _u):
        pytest.fail("group_member_check should not run when user is in approver_user_ids")

    res = slack_socket.handle_block_actions(
        _payload("agent_notify_approve", user_id="U_OK", channel_id="C_TEAM"),
        _slack_config(approver_user_groups=("S_ONCALL",)),
        update_fn=lambda *a, **kw: None,
        group_member_check=_should_not_be_called,
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


# --- daemon-side helpers ---------------------------------------------------


def test_interactive_workspaces_picks_only_actionable():
    from coding_agent_notifier import config as cfgmod

    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "home": {  # webhook-only, no actionable
                    "enabled": True,
                    "bot_token": "xoxb-h",
                },
                "work": {
                    "enabled": True,
                    "bot_token": "xoxb-w",
                    "app_token": "xapp-w",
                    "actionable_approvals": True,
                    "approver_user_ids": ["U01"],
                },
            },
        },
    })
    selected = slack_socket._interactive_workspaces(cfg)
    names = [n for n, _ in selected]
    assert names == ["work"]


def test_interactive_workspaces_back_compat_legacy_default():
    from coding_agent_notifier import config as cfgmod

    # Legacy [sinks.slack] actionable setup — still picked up.
    cfg = cfgmod.parse_config({
        "sinks": {
            "slack": {
                "enabled": True,
                "bot_token": "xoxb-l",
                "app_token": "xapp-l",
                "actionable_approvals": True,
                "approver_user_ids": ["U01"],
            },
        },
    })
    selected = slack_socket._interactive_workspaces(cfg)
    names = [n for n, _ in selected]
    assert names == ["default"]


class _FakeSlackResponse:
    def __init__(self, data: dict):
        self.data = data


class _FakeWebClient:
    def __init__(self):
        self.calls: list[str] = []

    def usergroups_users_list(self, *, usergroup: str) -> _FakeSlackResponse:
        self.calls.append(usergroup)
        return _FakeSlackResponse({"ok": True, "users": ["U_A", "U_B"]})


def test_group_member_check_caches_within_ttl():
    web = _FakeWebClient()
    check = slack_socket._make_group_member_check(web)
    assert check("S_ONCALL", "U_A") is True
    assert check("S_ONCALL", "U_B") is True
    assert check("S_ONCALL", "U_C") is False
    # All three clicks resolved from a single API call.
    assert web.calls == ["S_ONCALL"]


def test_group_member_check_raises_on_api_failure():
    class _FailingClient:
        def usergroups_users_list(self, *, usergroup):
            return _FakeSlackResponse({"ok": False, "error": "ratelimited"})

    check = slack_socket._make_group_member_check(_FailingClient())
    with pytest.raises(RuntimeError, match="ratelimited"):
        check("S_ONCALL", "U_A")


def test_group_member_check_caches_per_group():
    web = _FakeWebClient()
    check = slack_socket._make_group_member_check(web)
    check("S_ONE", "U_A")
    check("S_TWO", "U_A")
    check("S_ONE", "U_B")
    # One API call per distinct group, cached afterward.
    assert web.calls == ["S_ONE", "S_TWO"]
