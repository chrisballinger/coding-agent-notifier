from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coding_agent_notifier import expandable_messages as em
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


def test_click_on_timed_out_record_is_noop(tmp_path: Path):
    """Once the hook timed out and fell through to the TUI, a late Slack
    click can't change Claude Code's behavior — the hook process is gone.
    Treat the click as a no-op (with an ephemeral hint), don't try to
    resolve, and don't overwrite the resolved-elsewhere body that
    PostToolUse will write."""
    pa.create(
        "appr-timeout",
        agent="claude-code",
        session_id="s",
        tool_name="AskUserQuestion",
        tool_input={"questions": [{"question": "Q?", "options": [{"label": "A"}, {"label": "B"}]}]},
        base_dir=tmp_path,
    )
    pa.set_message_ref("appr-timeout", "C1", "1.0", base_dir=tmp_path)
    pa.resolve("appr-timeout", "timed_out", actor="timeout", base_dir=tmp_path)

    updates: list[dict] = []
    ephemerals: list[dict] = []

    def _update(bot_token, channel, ts, body):
        updates.append({"channel": channel, "ts": ts, "body": body})

    def _ephemeral(*, channel, user, text):
        ephemerals.append({"channel": channel, "user": user, "text": text})

    payload = _payload("agent_notify_option_0", value="appr-timeout")
    res = slack_socket.handle_block_actions(
        payload,
        _slack_config(),
        update_fn=_update,
        ephemeral_fn=_ephemeral,
        base_dir=tmp_path,
    )

    assert res.handled is True
    assert res.rejected_reason == "timed_out"
    # No chat.update — PostToolUse owns the resolution rendering.
    assert updates == []
    # User got an ephemeral hint redirecting them to TUI / Remote.
    assert len(ephemerals) == 1
    assert "terminal" in ephemerals[0]["text"].lower()
    # Record stays in `timed_out` state (not upgraded to allow/deny by
    # the click).
    rec = pa.read("appr-timeout", base_dir=tmp_path)
    assert rec["decision"] == "timed_out"


def test_unknown_action_id_not_handled():
    payload = _payload("some_other_button")
    res = slack_socket.handle_block_actions(payload, _slack_config())
    assert res.handled is False


def test_option_click_resolves_with_selected_index(tmp_path: Path):
    """An AskUserQuestion option-button click resolves with allow + the
    selected option index, and the chat.update body uses 'Selected `<label>`'
    wording."""
    pa.create(
        "appr-aq",
        agent="claude-code",
        session_id="s",
        tool_name="AskUserQuestion",
        tool_input={
            "questions": [
                {
                    "question": "Q?",
                    "options": [
                        {"label": "First"},
                        {"label": "Second"},
                        {"label": "Third"},
                    ],
                }
            ]
        },
        base_dir=tmp_path,
    )
    pa.set_message_ref("appr-aq", "C1", "1.0", base_dir=tmp_path)

    updates: list[dict] = []

    def _update(bot_token, channel, ts, body):
        updates.append({"channel": channel, "ts": ts, "body": body})

    payload = _payload("agent_notify_option_2", value="appr-aq")
    res = slack_socket.handle_block_actions(
        payload,
        _slack_config(),
        update_fn=_update,
        base_dir=tmp_path,
    )

    assert res.handled is True
    assert res.decision == "allow"
    rec = pa.read("appr-aq", base_dir=tmp_path)
    assert rec["decision"] == "allow"
    assert rec["selected_option_index"] == 2
    # chat.update body uses the selected label.
    assert len(updates) == 1
    body_text = updates[0]["body"]["text"]
    assert "Selected `Third`" in body_text


def test_option_click_with_invalid_index_suffix_not_handled():
    """If the action_id suffix isn't an integer, treat as unknown."""
    payload = _payload("agent_notify_option_xyz", value="appr-1")
    res = slack_socket.handle_block_actions(payload, _slack_config())
    assert res.handled is False


def test_multi_question_partial_click_records_without_resolving(tmp_path: Path):
    """For a 2-question AskUserQuestion, clicking ONE option records the
    answer but does NOT mark the approval resolved — wait() should keep
    blocking until the other question is also answered."""
    pa.create(
        "appr-mq",
        agent="claude-code",
        session_id="s",
        tool_name="AskUserQuestion",
        tool_input={
            "questions": [
                {"question": "Q1?", "options": [{"label": "A1"}, {"label": "B1"}]},
                {"question": "Q2?", "options": [{"label": "A2"}, {"label": "B2"}]},
            ]
        },
        base_dir=tmp_path,
    )
    pa.set_message_ref("appr-mq", "C1", "1.0", base_dir=tmp_path)
    updates: list[dict] = []
    def _update(bot_token, channel, ts, body):
        updates.append({"body": body})

    # Click Q1 → option B1 (index 1).
    payload = _payload("agent_notify_option_0_1", value="appr-mq")
    res = slack_socket.handle_block_actions(
        payload, _slack_config(),
        update_fn=_update,
        base_dir=tmp_path,
    )
    assert res.handled is True
    # No final decision yet.
    assert res.decision is None

    rec = pa.read("appr-mq", base_dir=tmp_path)
    assert rec["decision"] is None
    assert rec["selected_options"] == {"0": 1}

    # The chat.update body re-rendered the approval message with ✓ on
    # the answered Q1 option — buttons remain tappable for Q2.
    assert len(updates) == 1
    blocks = updates[0]["body"]["attachments"][0]["blocks"]
    actions_blocks = [b for b in blocks if b.get("type") == "actions"]
    assert len(actions_blocks) == 3  # Q1 + Q2 + Deny still present


def test_suggestion_click_resolves_with_selected_index(tmp_path: Path):
    """A permission_suggestion click resolves the approval with allow +
    selected_suggestion_index, and the chat.update body uses
    'Approved & applied …' wording derived from the suggestion label."""
    pa.create(
        "appr-sugg",
        agent="claude-code",
        session_id="s",
        tool_name="Bash",
        tool_input={"command": "curl https://example.invalid/install.sh | bash"},
        permission_suggestions=[
            {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "curl:*"}],
             "behavior": "allow", "destination": "localSettings"},
        ],
        base_dir=tmp_path,
    )
    pa.set_message_ref("appr-sugg", "C1", "1.0", base_dir=tmp_path)
    updates: list[dict] = []
    def _update(bot_token, channel, ts, body):
        updates.append({"body": body})

    payload = _payload("agent_notify_suggestion_0", value="appr-sugg")
    res = slack_socket.handle_block_actions(
        payload, _slack_config(),
        update_fn=_update,
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.decision == "allow"
    rec = pa.read("appr-sugg", base_dir=tmp_path)
    assert rec["decision"] == "allow"
    assert rec["selected_suggestion_index"] == 0
    # chat.update body uses the derived suggestion label.
    assert len(updates) == 1
    body_text = updates[0]["body"]["text"]
    assert "Approved & applied" in body_text


def test_suggestion_click_with_invalid_index_suffix_not_handled():
    payload = _payload("agent_notify_suggestion_xyz", value="appr-1")
    res = slack_socket.handle_block_actions(payload, _slack_config())
    assert res.handled is False


def test_multi_question_final_click_resolves_with_selected_options(tmp_path: Path):
    """When the second-and-last answer comes in, the daemon resolves with
    the full selected_options dict and the chat.update uses
    build_resolved_message (no buttons)."""
    pa.create(
        "appr-mq2",
        agent="claude-code",
        session_id="s",
        tool_name="AskUserQuestion",
        tool_input={
            "questions": [
                {"question": "Q1?", "options": [{"label": "A1"}, {"label": "B1"}]},
                {"question": "Q2?", "options": [{"label": "A2"}, {"label": "B2"}]},
            ]
        },
        base_dir=tmp_path,
    )
    pa.set_message_ref("appr-mq2", "C1", "1.0", base_dir=tmp_path)

    def _update(bot_token, channel, ts, body):
        pass

    # First click: Q1 → A1.
    slack_socket.handle_block_actions(
        _payload("agent_notify_option_0_0", value="appr-mq2"),
        _slack_config(),
        update_fn=_update,
        base_dir=tmp_path,
    )
    rec = pa.read("appr-mq2", base_dir=tmp_path)
    assert rec["decision"] is None  # still partial

    # Second click: Q2 → B2. Resolves the approval.
    res = slack_socket.handle_block_actions(
        _payload("agent_notify_option_1_1", value="appr-mq2"),
        _slack_config(),
        update_fn=_update,
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.decision == "allow"

    rec = pa.read("appr-mq2", base_dir=tmp_path)
    assert rec["decision"] == "allow"
    assert rec["selected_options"] == {"0": 0, "1": 1}


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


def test_interactive_workspaces_picks_up_default_workspace():
    from coding_agent_notifier import config as cfgmod

    # The "default" name has no special meaning at parse time — it's
    # selected by the daemon the same way any other named workspace is.
    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "default": {
                    "enabled": True,
                    "bot_token": "xoxb-d",
                    "app_token": "xapp-d",
                    "actionable_approvals": True,
                    "approver_user_ids": ["U01"],
                },
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


# ----------------------------------------------------------------------
# Modal trigger + view_submission flow
# ----------------------------------------------------------------------


def _ask_user_question_record(approval_id: str, *, base_dir: Path,
                              questions: list[dict] | None = None) -> None:
    """Helper: write an AskUserQuestion approval record + message ref."""
    pa.create(
        approval_id,
        agent="claude-code",
        session_id="s",
        tool_name="AskUserQuestion",
        tool_input={
            "questions": questions or [
                {
                    "question": "Mascot?",
                    "options": [{"label": "Raccoon"}, {"label": "Capybara"}],
                }
            ]
        },
        base_dir=base_dir,
    )
    pa.set_message_ref(approval_id, "C1", "1.0", base_dir=base_dir)


def _modal_payload(callback_id: str, *, approval_id: str,
                   text: str, question_index: int | None = None,
                   user_id: str = "U_OK") -> dict:
    """Build a Slack view_submission payload matching the daemon's expected shape."""
    import json as _json
    metadata: dict[str, Any] = {"approval_id": approval_id}
    if question_index is not None:
        metadata["question_index"] = question_index
    return {
        "type": "view_submission",
        "user": {"id": user_id},
        "view": {
            "callback_id": callback_id,
            "private_metadata": _json.dumps(metadata),
            "state": {
                "values": {
                    "agent_notify_modal_input": {
                        "agent_notify_modal_input_value": {
                            "type": "plain_text_input",
                            "value": text,
                        }
                    }
                }
            },
        },
    }


def test_custom_answer_button_opens_modal_without_resolving(tmp_path: Path):
    """Tapping the ✏️ Custom answer button calls views_open with a modal
    pre-loaded with the question text — and does NOT resolve the approval
    (the resolve happens later on view_submission)."""
    _ask_user_question_record("appr-custom", base_dir=tmp_path)
    opened: list[dict] = []

    def _views_open(trigger_id, view):
        opened.append({"trigger_id": trigger_id, "view": view})

    payload = _payload("agent_notify_custom_answer_0", value="appr-custom")
    payload["trigger_id"] = "trig-001"
    res = slack_socket.handle_block_actions(
        payload, _slack_config(),
        views_open_fn=_views_open,
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.decision is None  # Modal-trigger doesn't resolve.
    rec = pa.read("appr-custom", base_dir=tmp_path)
    assert rec["decision"] is None
    assert len(opened) == 1
    view = opened[0]["view"]
    assert view["callback_id"] == "agent_notify_modal_custom_answer"
    # Question text appears in the modal so the user knows what they're answering.
    assert any(
        "Mascot" in (b.get("text", {}).get("text", ""))
        for b in view["blocks"]
    )


def test_deny_reason_button_opens_modal_without_resolving(tmp_path: Path):
    """Tapping 💬 Deny with reason opens the modal without denying."""
    pa.create(
        "appr-dr", agent="claude-code", session_id="s",
        tool_name="Bash", tool_input={"command": "npm test"},
        base_dir=tmp_path,
    )
    pa.set_message_ref("appr-dr", "C1", "1.0", base_dir=tmp_path)
    opened: list[dict] = []

    def _views_open(trigger_id, view):
        opened.append({"trigger_id": trigger_id, "view": view})

    payload = _payload("agent_notify_deny_reason", value="appr-dr")
    payload["trigger_id"] = "trig-002"
    res = slack_socket.handle_block_actions(
        payload, _slack_config(),
        views_open_fn=_views_open,
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.decision is None
    rec = pa.read("appr-dr", base_dir=tmp_path)
    assert rec["decision"] is None
    assert len(opened) == 1
    assert opened[0]["view"]["callback_id"] == "agent_notify_modal_deny_reason"


def test_custom_answer_modal_trigger_without_views_open_warns(tmp_path: Path):
    """If views_open_fn isn't wired (defensive), the click still ACKs but
    leaves the approval pending — the user can fall back to one-tap option."""
    _ask_user_question_record("appr-no-views", base_dir=tmp_path)
    payload = _payload("agent_notify_custom_answer_0", value="appr-no-views")
    payload["trigger_id"] = "trig-003"
    res = slack_socket.handle_block_actions(
        payload, _slack_config(), base_dir=tmp_path,  # no views_open_fn
    )
    assert res.handled is True
    assert res.decision is None
    assert res.rejected_reason == "no_trigger"
    rec = pa.read("appr-no-views", base_dir=tmp_path)
    assert rec["decision"] is None


def test_custom_answer_modal_trigger_for_unknown_approval(tmp_path: Path):
    """Modal trigger for an approval that no longer exists → stale ephemeral."""
    ephemerals: list[dict] = []

    def _ephemeral(channel, user, text):
        ephemerals.append({"channel": channel, "user": user, "text": text})

    payload = _payload("agent_notify_custom_answer_0", value="appr-gone")
    payload["trigger_id"] = "trig-004"
    res = slack_socket.handle_block_actions(
        payload, _slack_config(),
        ephemeral_fn=_ephemeral,
        views_open_fn=lambda t, v: None,
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.rejected_reason == "unknown_approval"
    assert ephemerals and "no longer pending" in ephemerals[0]["text"]


def test_view_submission_custom_answer_single_question_resolves(tmp_path: Path):
    """For a single-question AskUserQuestion, submitting the custom-answer
    modal resolves immediately with the typed text in `freeform_answers`."""
    _ask_user_question_record("appr-single-cust", base_dir=tmp_path)
    updates: list[dict] = []

    def _update(bot_token, channel, ts, body):
        updates.append({"channel": channel, "ts": ts, "body": body})

    payload = _modal_payload(
        "agent_notify_modal_custom_answer",
        approval_id="appr-single-cust",
        text="Definitely a raccoon",
        question_index=0,
    )
    res = slack_socket.handle_view_submission(
        payload, _slack_config(),
        update_fn=_update, base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.decision == "allow"
    rec = pa.read("appr-single-cust", base_dir=tmp_path)
    assert rec["decision"] == "allow"
    assert rec["freeform_answers"] == {"0": "Definitely a raccoon"}
    assert rec["actor"] == "U_OK"
    # The chat.update body's Q→A summary contains the typed text.
    assert len(updates) == 1
    body = updates[0]["body"]
    assert "Answered by <@U_OK>" in body["text"]
    section_texts = [
        b["text"]["text"]
        for b in body["attachments"][0]["blocks"]
        if b.get("type") == "section"
    ]
    assert any("Definitely a raccoon" in t for t in section_texts)


def test_view_submission_custom_answer_multi_question_partial(tmp_path: Path):
    """Submitting custom answer for 1 of 2 questions records the text but
    doesn't resolve — the approval still needs Q2."""
    _ask_user_question_record(
        "appr-multi-cust", base_dir=tmp_path,
        questions=[
            {"question": "Mascot?", "options": [{"label": "Raccoon"}, {"label": "Capybara"}]},
            {"question": "Color?", "options": [{"label": "Green"}, {"label": "Yellow"}]},
        ],
    )
    payload = _modal_payload(
        "agent_notify_modal_custom_answer",
        approval_id="appr-multi-cust",
        text="A pangolin",
        question_index=0,
    )
    res = slack_socket.handle_view_submission(
        payload, _slack_config(),
        update_fn=lambda *a, **kw: None, base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.decision is None  # Q2 still unanswered.
    rec = pa.read("appr-multi-cust", base_dir=tmp_path)
    assert rec["decision"] is None
    assert rec["freeform_answers"] == {"0": "A pangolin"}


def test_view_submission_custom_answer_multi_finalizes_when_all_answered(tmp_path: Path):
    """When custom-answer fills the last unanswered question, the approval
    resolves with both option-click and freeform answers in one go."""
    _ask_user_question_record(
        "appr-mix", base_dir=tmp_path,
        questions=[
            {"question": "Mascot?", "options": [{"label": "Raccoon"}, {"label": "Capybara"}]},
            {"question": "Color?", "options": [{"label": "Green"}, {"label": "Yellow"}]},
        ],
    )
    # Pre-record an option click for Q2.
    pa.record_partial_answer(
        "appr-mix", 1, option_index=1, actor="U_OK", base_dir=tmp_path,
    )
    payload = _modal_payload(
        "agent_notify_modal_custom_answer",
        approval_id="appr-mix",
        text="A pangolin",
        question_index=0,
    )
    res = slack_socket.handle_view_submission(
        payload, _slack_config(),
        update_fn=lambda *a, **kw: None, base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.decision == "allow"
    rec = pa.read("appr-mix", base_dir=tmp_path)
    assert rec["decision"] == "allow"
    assert rec["freeform_answers"] == {"0": "A pangolin"}
    assert rec["selected_options"] == {"1": 1}


def test_view_submission_deny_reason_resolves_with_message(tmp_path: Path):
    """Submitting the deny-reason modal resolves the approval as deny + carries
    the typed text into `deny_reason`."""
    pa.create(
        "appr-deny-text", agent="claude-code", session_id="s",
        tool_name="Bash", tool_input={"command": "npm test"},
        base_dir=tmp_path,
    )
    pa.set_message_ref("appr-deny-text", "C1", "1.0", base_dir=tmp_path)
    updates: list[dict] = []

    def _update(bot_token, channel, ts, body):
        updates.append({"channel": channel, "ts": ts, "body": body})

    payload = _modal_payload(
        "agent_notify_modal_deny_reason",
        approval_id="appr-deny-text",
        text="check the lockfile first",
    )
    res = slack_socket.handle_view_submission(
        payload, _slack_config(),
        update_fn=_update, base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.decision == "deny"
    rec = pa.read("appr-deny-text", base_dir=tmp_path)
    assert rec["decision"] == "deny"
    assert rec["deny_reason"] == "check the lockfile first"
    # Resolved-message render shows the reason.
    assert len(updates) == 1
    blocks = updates[0]["body"]["attachments"][0]["blocks"]
    rendered = "\n".join(
        b.get("elements", [{}])[0].get("text", "") if b.get("type") == "context"
        else b.get("text", {}).get("text", "")
        for b in blocks
    )
    assert "check the lockfile first" in rendered


def test_view_submission_unknown_approval(tmp_path: Path):
    """view_submission for an approval that no longer exists is a no-op."""
    payload = _modal_payload(
        "agent_notify_modal_deny_reason",
        approval_id="appr-vanished",
        text="too late",
    )
    res = slack_socket.handle_view_submission(
        payload, _slack_config(),
        update_fn=lambda *a, **kw: None, base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.decision is None
    assert res.rejected_reason == "unknown_approval"


def test_view_submission_bad_metadata(tmp_path: Path):
    """Malformed private_metadata → handled=True with rejected_reason."""
    payload = {
        "type": "view_submission",
        "user": {"id": "U_OK"},
        "view": {
            "callback_id": "agent_notify_modal_deny_reason",
            "private_metadata": "not-json",
            "state": {
                "values": {
                    "agent_notify_modal_input": {
                        "agent_notify_modal_input_value": {"value": "anything"}
                    }
                }
            },
        },
    }
    res = slack_socket.handle_view_submission(payload, _slack_config(), base_dir=tmp_path)
    assert res.handled is True
    assert res.rejected_reason == "bad_metadata"


def test_view_submission_unknown_callback_not_handled(tmp_path: Path):
    """A view_submission with a callback_id we don't know about is not ours."""
    payload = _modal_payload(
        "some_other_modal",
        approval_id="appr-x", text="anything",
    )
    res = slack_socket.handle_view_submission(payload, _slack_config(), base_dir=tmp_path)
    assert res.handled is False


# ---------------------------------------------------------------------
# Show more / Show less toggle dispatch
# ---------------------------------------------------------------------


def _create_expandable(tmp_path: Path, msg_id: str = "msg-1") -> dict:
    return em.create(
        msg_id,
        workspace="default",
        channel="D0123",
        message_ts="1700000000.000100",
        preview_body={"text": "preview", "attachments": [{"color": "#fff", "blocks": []}]},
        full_body={"text": "full", "attachments": [{"color": "#fff", "blocks": []}]},
        base_dir=tmp_path,
    ) and em.read(msg_id, base_dir=tmp_path)


def test_expand_click_chat_updates_with_full_body(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(em, "default_dir", lambda: tmp_path)
    _create_expandable(tmp_path)

    updates: list[dict] = []
    payload = _payload("agent_notify_expand", value="msg-1")
    res = slack_socket.handle_block_actions(
        payload, _slack_config(),
        update_fn=lambda token, ch, ts, body: updates.append({"ch": ch, "ts": ts, "body": body}),
    )
    assert res.handled is True
    assert res.rejected_reason is None
    assert len(updates) == 1
    assert updates[0]["ch"] == "D0123"
    assert updates[0]["ts"] == "1700000000.000100"
    assert updates[0]["body"]["text"] == "full"


def test_collapse_click_chat_updates_with_preview_body(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(em, "default_dir", lambda: tmp_path)
    _create_expandable(tmp_path)

    updates: list[dict] = []
    payload = _payload("agent_notify_collapse", value="msg-1")
    res = slack_socket.handle_block_actions(
        payload, _slack_config(),
        update_fn=lambda token, ch, ts, body: updates.append({"body": body}),
    )
    assert res.handled is True
    assert updates[0]["body"]["text"] == "preview"


def test_toggle_click_with_unknown_message_id_returns_unknown_toggle(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(em, "default_dir", lambda: tmp_path)
    updates: list[dict] = []
    payload = _payload("agent_notify_expand", value="ghost-id")
    res = slack_socket.handle_block_actions(
        payload, _slack_config(),
        update_fn=lambda *a, **kw: updates.append(a),
    )
    assert res.handled is True
    assert res.rejected_reason == "unknown_toggle"
    assert updates == []


def test_toggle_click_unauthorized_user_rejected_before_lookup(
    tmp_path: Path, monkeypatch,
):
    """Auth runs before the toggle branch — record stays untouched."""
    monkeypatch.setattr(em, "default_dir", lambda: tmp_path)
    _create_expandable(tmp_path)
    updates: list[dict] = []
    payload = _payload("agent_notify_expand", value="msg-1", user_id="U_OUTSIDER")
    res = slack_socket.handle_block_actions(
        payload, _slack_config(),  # approver_user_ids=("U_OK",) — U_OUTSIDER not allowed
        update_fn=lambda *a, **kw: updates.append(a),
    )
    assert res.handled is True
    assert res.rejected_reason is not None  # auth rejection
    assert updates == []


def test_view_submission_empty_text_noop(tmp_path: Path):
    """Empty text input (defensive against Slack's min_length escaping) is a no-op."""
    pa.create(
        "appr-empty", agent="claude-code", session_id="s",
        tool_name="Bash", tool_input={"command": "x"},
        base_dir=tmp_path,
    )
    payload = _modal_payload(
        "agent_notify_modal_deny_reason",
        approval_id="appr-empty", text="",
    )
    res = slack_socket.handle_view_submission(payload, _slack_config(), base_dir=tmp_path)
    assert res.handled is True
    assert res.rejected_reason == "empty_text"
    rec = pa.read("appr-empty", base_dir=tmp_path)
    assert rec["decision"] is None


# ---------------------------------------------------------------------
# freeform_text="deny" defense-in-depth gates
# ---------------------------------------------------------------------


def test_modal_trigger_blocked_when_freeform_disabled_custom_answer(tmp_path: Path):
    """A stale Custom-answer button click on a freeform-disabled workspace
    must NOT open the modal; instead an ephemeral hint goes back to the
    channel, and the approval stays untouched."""
    _ask_user_question_record("appr-block-cust", base_dir=tmp_path)
    opened: list[dict] = []
    ephemerals: list[dict] = []

    payload = _payload("agent_notify_custom_answer_0", value="appr-block-cust")
    payload["trigger_id"] = "trig-blocked"
    res = slack_socket.handle_block_actions(
        payload, _slack_config(freeform_text="deny"),
        ephemeral_fn=lambda **kw: ephemerals.append(kw),
        views_open_fn=lambda t, v: opened.append({"trigger_id": t, "view": v}),
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.rejected_reason == "freeform_disabled"
    assert opened == []  # modal NOT opened
    rec = pa.read("appr-block-cust", base_dir=tmp_path)
    assert rec["decision"] is None  # approval still pending
    assert len(ephemerals) == 1
    assert ephemerals[0]["user"] == "U_OK"
    assert "freeform" in ephemerals[0]["text"].lower()


def test_modal_trigger_blocked_when_freeform_disabled_deny_reason(tmp_path: Path):
    """Same gate applies to the Deny-with-reason button."""
    pa.create(
        "appr-block-dr", agent="claude-code", session_id="s",
        tool_name="Bash",
        tool_input={"command": "curl https://example.invalid/install.sh | bash"},
        base_dir=tmp_path,
    )
    pa.set_message_ref("appr-block-dr", "C1", "1.0", base_dir=tmp_path)
    opened: list[dict] = []
    ephemerals: list[dict] = []

    payload = _payload("agent_notify_deny_reason", value="appr-block-dr")
    payload["trigger_id"] = "trig-blocked-2"
    res = slack_socket.handle_block_actions(
        payload, _slack_config(freeform_text="deny"),
        ephemeral_fn=lambda **kw: ephemerals.append(kw),
        views_open_fn=lambda t, v: opened.append({"t": t, "v": v}),
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.rejected_reason == "freeform_disabled"
    assert opened == []
    rec = pa.read("appr-block-dr", base_dir=tmp_path)
    assert rec["decision"] is None
    assert len(ephemerals) == 1


def test_view_submission_blocked_when_freeform_disabled_custom_answer(tmp_path: Path):
    """A stale custom-answer modal that submits after the policy was
    flipped to "deny" must be dropped without resolving."""
    _ask_user_question_record("appr-stale-cust", base_dir=tmp_path)
    resolved: list[dict] = []
    ephemerals: list[dict] = []

    def _resolve(*args, **kwargs):
        resolved.append({"args": args, "kwargs": kwargs})
        return {"decision": "allow"}  # would have resolved if not gated

    payload = _modal_payload(
        "agent_notify_modal_custom_answer",
        approval_id="appr-stale-cust",
        text="leak this please",
        question_index=0,
    )
    res = slack_socket.handle_view_submission(
        payload, _slack_config(freeform_text="deny"),
        resolve_fn=_resolve,
        ephemeral_fn=lambda **kw: ephemerals.append(kw),
        update_fn=lambda *a, **kw: None,
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.rejected_reason == "freeform_disabled"
    assert resolved == []  # resolve NOT called
    rec = pa.read("appr-stale-cust", base_dir=tmp_path)
    assert rec["decision"] is None
    assert len(ephemerals) == 1
    # Channel from the pending approval record (set_message_ref → "C1").
    assert ephemerals[0]["channel"] == "C1"
    assert "freeform" in ephemerals[0]["text"].lower()


def test_view_submission_blocked_when_freeform_disabled_deny_reason(tmp_path: Path):
    pa.create(
        "appr-stale-dr", agent="claude-code", session_id="s",
        tool_name="Bash", tool_input={"command": "x"},
        base_dir=tmp_path,
    )
    pa.set_message_ref("appr-stale-dr", "C1", "1.0", base_dir=tmp_path)
    resolved: list[dict] = []
    ephemerals: list[dict] = []

    def _resolve(*args, **kwargs):
        resolved.append({"args": args, "kwargs": kwargs})
        return {"decision": "deny"}

    payload = _modal_payload(
        "agent_notify_modal_deny_reason",
        approval_id="appr-stale-dr",
        text="reason text that should not reach the agent",
    )
    res = slack_socket.handle_view_submission(
        payload, _slack_config(freeform_text="deny"),
        resolve_fn=_resolve,
        ephemeral_fn=lambda **kw: ephemerals.append(kw),
        update_fn=lambda *a, **kw: None,
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.rejected_reason == "freeform_disabled"
    assert resolved == []
    rec = pa.read("appr-stale-dr", base_dir=tmp_path)
    assert rec["decision"] is None
    assert len(ephemerals) == 1


def test_view_submission_block_swallows_ephemeral_failure(tmp_path: Path):
    """Hooks-must-never-block: if the ephemeral helper raises, the gate
    still returns handled=True (the freeform_disabled response is more
    important than the hint succeeding)."""
    _ask_user_question_record("appr-eph-fail", base_dir=tmp_path)

    def _bad_ephemeral(**kw):
        raise RuntimeError("Slack 500")

    payload = _modal_payload(
        "agent_notify_modal_custom_answer",
        approval_id="appr-eph-fail", text="x", question_index=0,
    )
    res = slack_socket.handle_view_submission(
        payload, _slack_config(freeform_text="deny"),
        ephemeral_fn=_bad_ephemeral,
        update_fn=lambda *a, **kw: None,
        base_dir=tmp_path,
    )
    assert res.handled is True
    assert res.rejected_reason == "freeform_disabled"
    rec = pa.read("appr-eph-fail", base_dir=tmp_path)
    assert rec["decision"] is None
