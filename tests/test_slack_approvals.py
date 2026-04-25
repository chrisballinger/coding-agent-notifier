from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coding_agent_notifier.config import SlackConfig
from coding_agent_notifier.event import Event
from coding_agent_notifier.sinks import slack as slack_mod
from coding_agent_notifier.sinks.base import SinkError
from coding_agent_notifier.sinks.slack import (
    APPROVE_ACTION_ID,
    DENY_ACTION_ID,
    SUGGESTION_ACTION_ID_PREFIX,
    build_approval_message,
    build_resolved_message,
    post_approval_message,
    update_message,
)


def _event(**kw) -> Event:
    base: dict[str, Any] = dict(
        agent="claude-code",
        kind="permission",
        message="",
        cwd=Path("/Users/me/myproj"),
        session_id="abc123def456",
        tool_name="Bash",
        tool_input={"command": "echo hi"},
        source_app="iTerm2",
    )
    base.update(kw)
    return Event(**base)


class _FakePoster:
    def __init__(self, *, status: int = 200, body: dict | None = None):
        self.status = status
        self.body = body if body is not None else {"ok": True, "ts": "1.000", "channel": "C123"}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, payload: dict, *, headers: dict[str, str] | None = None,
                 timeout: float = 10.0) -> tuple[int, str]:
        self.calls.append({"url": url, "payload": payload, "headers": headers})
        return self.status, json.dumps(self.body)


def test_build_approval_message_adds_actions_block():
    body = build_approval_message(_event(), "appr-123")
    actions = _find_block(body, "actions")
    assert actions is not None
    labels = [el["text"]["text"] for el in actions["elements"]]
    assert labels == ["Approve", "Deny"]


def test_build_approval_message_carries_approval_id_in_button_value():
    body = build_approval_message(_event(), "appr-42")
    actions = _find_block(body, "actions")
    for el in actions["elements"]:
        assert el["value"] == "appr-42"


def test_build_approval_message_sets_action_ids():
    body = build_approval_message(_event(), "appr-1")
    actions = _find_block(body, "actions")
    action_ids = [el["action_id"] for el in actions["elements"]]
    assert action_ids == [APPROVE_ACTION_ID, DENY_ACTION_ID]


def test_approve_button_has_confirm_dialog():
    body = build_approval_message(_event(), "appr-1")
    actions = _find_block(body, "actions")
    approve = next(el for el in actions["elements"] if el["action_id"] == APPROVE_ACTION_ID)
    assert "confirm" in approve
    # Confirm text mentions the tool so an accidental-tap user sees context.
    assert "Bash" in approve["confirm"]["text"]["text"]


def test_deny_button_has_no_confirm():
    body = build_approval_message(_event(), "appr-1")
    actions = _find_block(body, "actions")
    deny = next(el for el in actions["elements"] if el["action_id"] == DENY_ACTION_ID)
    # Deny is the safe direction — no confirm dialog, one tap.
    assert "confirm" not in deny


def test_post_approval_message_returns_channel_and_ts():
    cfg = SlackConfig(enabled=True, bot_token="xoxb-test", channel="C0XX", interactive=True)
    poster = _FakePoster(body={"ok": True, "ts": "1735689600.0042", "channel": "C0XX"})
    channel, ts = post_approval_message(_event(), cfg, "appr-99", poster=poster)
    assert channel == "C0XX"
    assert ts == "1735689600.0042"
    # One call, to chat.postMessage
    assert len(poster.calls) == 1
    assert poster.calls[0]["url"].endswith("/chat.postMessage")
    assert poster.calls[0]["headers"]["Authorization"] == "Bearer xoxb-test"


def test_post_approval_message_dms_installing_user_when_channel_is_me():
    """`@me` resolves to approver_user_ids[0] (the installing user) — NOT the
    bot's own user_id from auth.test. Posting to the installing user's id
    opens a real DM thread; posting to the bot's id lands in App Home and is
    invisible without the messages_tab manifest flag.

    auth.test must NOT be called when approvers are configured.
    """
    cfg = SlackConfig(
        enabled=True,
        bot_token="xoxb-test",
        channel="@me",
        interactive=True,
        approver_user_ids=("U_USER",),
    )

    class _SeqPoster:
        def __init__(self):
            self.calls: list[dict] = []
        def __call__(self, url, payload, *, headers=None, timeout=10.0):
            self.calls.append({"url": url, "payload": payload})
            return 200, json.dumps({"ok": True, "ts": "2.0", "channel": "U_USER"})

    poster = _SeqPoster()
    channel, ts = post_approval_message(_event(), cfg, "appr-1", poster=poster)
    assert channel == "U_USER"
    # Single call to chat.postMessage — auth.test is bypassed.
    assert len(poster.calls) == 1
    assert poster.calls[0]["url"].endswith("/chat.postMessage")
    assert poster.calls[0]["payload"]["channel"] == "U_USER"


def test_post_approval_message_falls_back_to_self_when_no_approvers():
    """With approvers empty (degraded "DM-only-no-allowlist" mode), `@me`
    falls back to the bot's own user_id via auth.test. This lands in App
    Home Messages tab — visible only with the manifest flag enabled.
    """
    cfg = SlackConfig(
        enabled=True,
        bot_token="xoxb-test",
        channel="@me",
        interactive=True,
        approver_user_ids=(),  # empty — fallback path
    )

    class _SeqPoster:
        def __init__(self):
            self.calls: list[dict] = []
        def __call__(self, url, payload, *, headers=None, timeout=10.0):
            self.calls.append({"url": url, "payload": payload})
            if url.endswith("/auth.test"):
                return 200, json.dumps({"ok": True, "user_id": "U_BOT"})
            return 200, json.dumps({"ok": True, "ts": "2.0", "channel": "U_BOT"})

    poster = _SeqPoster()
    channel, ts = post_approval_message(_event(), cfg, "appr-1", poster=poster)
    assert channel == "U_BOT"
    assert poster.calls[0]["url"].endswith("/auth.test")
    assert poster.calls[1]["payload"]["channel"] == "U_BOT"


def test_post_approval_message_requires_bot_token():
    cfg = SlackConfig(enabled=True, webhook_url="https://hooks.slack.com/test", interactive=False)
    with pytest.raises(SinkError, match="bot_token"):
        post_approval_message(_event(), cfg, "appr-1", poster=_FakePoster())


def test_post_approval_message_raises_on_http_error():
    cfg = SlackConfig(enabled=True, bot_token="xoxb-test", channel="C1")
    poster = _FakePoster(status=500, body={"error": "boom"})
    with pytest.raises(SinkError, match="500"):
        post_approval_message(_event(), cfg, "appr-1", poster=poster)


def test_post_approval_message_raises_on_slack_api_error():
    cfg = SlackConfig(enabled=True, bot_token="xoxb-test", channel="C1")
    poster = _FakePoster(body={"ok": False, "error": "channel_not_found"})
    with pytest.raises(SinkError, match="channel_not_found"):
        post_approval_message(_event(), cfg, "appr-1", poster=poster)


def test_post_approval_message_raises_if_ts_missing():
    cfg = SlackConfig(enabled=True, bot_token="xoxb-test", channel="C1")
    poster = _FakePoster(body={"ok": True, "channel": "C1"})  # no ts
    with pytest.raises(SinkError, match="ts"):
        post_approval_message(_event(), cfg, "appr-1", poster=poster)


def test_build_resolved_message_allow():
    body = build_resolved_message(_event(), "allow", "@chris")
    att = body["attachments"][0]
    headers = [b for b in att["blocks"] if b["type"] == "section" and "Approved" in b["text"]["text"]]
    assert headers
    assert "@chris" in headers[0]["text"]["text"]
    # No actions block.
    assert not any(b["type"] == "actions" for b in att["blocks"])


def test_build_resolved_message_deny():
    body = build_resolved_message(_event(), "deny", "@alice")
    text = json.dumps(body)
    assert "Denied" in text
    assert "@alice" in text


def test_build_resolved_message_timeout():
    body = build_resolved_message(_event(), "timeout", "system")
    text = json.dumps(body)
    assert "Timed out" in text


# --- AskUserQuestion option-button rendering ---


def _ask_user_question_event(**overrides) -> Event:
    """Build a permission Event whose tool is AskUserQuestion."""
    tool_input = overrides.pop("tool_input", None) or {
        "questions": [
            {
                "question": "How should X be configured?",
                "header": "Routing shape",
                "multiSelect": False,
                "options": [
                    {"label": "Global config only", "description": "..."},
                    {"label": "Per-repo files", "description": "..."},
                    {"label": "Hybrid", "description": "..."},
                ],
            }
        ]
    }
    return _event(tool_name="AskUserQuestion", tool_input=tool_input, **overrides)


def test_build_approval_message_renders_option_buttons_for_ask_user_question():
    body = build_approval_message(_ask_user_question_event(), "appr-aq-1")
    # Multi-question render: each question gets its own actions block; a
    # final actions block holds the trailing Deny. With a single Q here,
    # we expect 2 actions blocks total (1 question + 1 deny).
    actions_blocks = _find_blocks(body, "actions")
    assert len(actions_blocks) == 2
    q1, deny_block = actions_blocks
    # Question 1's options are buttons; action_ids carry both q and o
    # indices so multi-question clicks are unambiguous.
    labels = [el["text"]["text"] for el in q1["elements"]]
    assert labels == ["Global config only", "Per-repo files", "Hybrid"]
    option_action_ids = [el["action_id"] for el in q1["elements"]]
    assert option_action_ids == [
        "agent_notify_option_0_0",
        "agent_notify_option_0_1",
        "agent_notify_option_0_2",
    ]
    # Deny lives in its own actions block at the end.
    assert deny_block["elements"][0]["action_id"] == DENY_ACTION_ID
    assert deny_block["elements"][0]["text"]["text"] == "Deny"
    # All buttons carry the approval_id as value.
    for el in q1["elements"] + deny_block["elements"]:
        assert el["value"] == "appr-aq-1"


def test_option_buttons_truncate_long_labels():
    long_label = "x" * 200
    ev = _ask_user_question_event(tool_input={
        "questions": [{
            "question": "Q?",
            "options": [{"label": long_label, "description": "..."}],
        }]
    })
    body = build_approval_message(ev, "appr-aq-2")
    actions = _find_block(body, "actions")
    text = actions["elements"][0]["text"]["text"]
    assert len(text) <= 75


def test_build_approval_message_renders_text_only_for_multiselect():
    """multiSelect questions can't be button-driven (no text input), so
    they're surfaced as the question header + a "answer in terminal"
    note — but the wrapper Q+Deny structure is still the AskUserQuestion
    flow (not the legacy Approve/Deny pair)."""
    ev = _ask_user_question_event(tool_input={
        "questions": [{
            "question": "Pick many",
            "multiSelect": True,
            "options": [{"label": "A"}, {"label": "B"}],
        }]
    })
    body = build_approval_message(ev, "appr-multi")
    actions_blocks = _find_blocks(body, "actions")
    # Only the trailing Deny block — no option buttons rendered for this
    # multiSelect question (user finishes in terminal).
    assert len(actions_blocks) == 1
    assert actions_blocks[0]["elements"][0]["action_id"] == DENY_ACTION_ID
    # The question header still appears as a section block.
    section_texts = [
        b["text"]["text"]
        for att in body.get("attachments", [])
        for b in att.get("blocks", [])
        if b.get("type") == "section"
    ]
    assert any("Pick many" in t for t in section_texts)


def test_build_approval_message_falls_back_for_malformed_payload():
    ev = _ask_user_question_event(tool_input={"questions": "not a list"})
    body = build_approval_message(ev, "appr-bad")
    actions = _find_block(body, "actions")
    labels = [el["text"]["text"] for el in actions["elements"]]
    assert labels == ["Approve", "Deny"]


def test_non_ask_user_question_still_renders_approve_deny():
    """Regression: Bash and other tools keep the standard two-button row."""
    body = build_approval_message(_event(), "appr-bash")  # _event() defaults to Bash
    actions = _find_block(body, "actions")
    labels = [el["text"]["text"] for el in actions["elements"]]
    assert labels == ["Approve", "Deny"]


def test_multi_question_renders_one_actions_block_per_question():
    """Multi-question AskUserQuestion: each question gets its own actions
    block (option buttons indexed by q,o), plus a single trailing Deny
    actions block at the end."""
    ev = _ask_user_question_event(tool_input={
        "questions": [
            {"question": "Q1?", "options": [{"label": "A1"}, {"label": "B1"}]},
            {"question": "Q2?", "options": [{"label": "A2"}, {"label": "B2"}, {"label": "C2"}]},
        ]
    })
    body = build_approval_message(ev, "appr-multi-1")
    actions_blocks = _find_blocks(body, "actions")
    assert len(actions_blocks) == 3  # Q1 + Q2 + Deny

    q1, q2, deny = actions_blocks
    q1_action_ids = [el["action_id"] for el in q1["elements"]]
    assert q1_action_ids == ["agent_notify_option_0_0", "agent_notify_option_0_1"]
    q2_action_ids = [el["action_id"] for el in q2["elements"]]
    assert q2_action_ids == [
        "agent_notify_option_1_0",
        "agent_notify_option_1_1",
        "agent_notify_option_1_2",
    ]
    assert deny["elements"][0]["action_id"] == DENY_ACTION_ID


def test_multi_question_renders_check_marks_for_answered_questions():
    """When `selected_options` is passed (chat.update during partial flow),
    the answered options get a ✓ prefix and the section header notes the
    answer."""
    ev = _ask_user_question_event(tool_input={
        "questions": [
            {"question": "Q1?", "options": [{"label": "A1"}, {"label": "B1"}]},
            {"question": "Q2?", "options": [{"label": "A2"}, {"label": "B2"}]},
        ]
    })
    body = build_approval_message(
        ev, "appr-partial-1", selected_options={"0": 1},  # answered Q1 with B1
    )
    actions_blocks = _find_blocks(body, "actions")
    q1 = actions_blocks[0]
    # Answered option (index 1) gets the ✓ prefix.
    assert q1["elements"][1]["text"]["text"].startswith("✓ ")
    # Unanswered option doesn't.
    assert not q1["elements"][0]["text"]["text"].startswith("✓ ")

    # Q2 still has no checks.
    q2 = actions_blocks[1]
    for el in q2["elements"]:
        assert not el["text"]["text"].startswith("✓ ")


def test_build_resolved_message_renders_qa_summary_for_multi_question():
    """Final chat.update for a multi-question approval lists every
    Q → answered-label pair so the resolved message captures the
    full decision."""
    ev = _ask_user_question_event(tool_input={
        "questions": [
            {"question": "Mascot?", "options": [{"label": "Raccoon"}, {"label": "Capybara"}]},
            {"question": "Color?", "options": [{"label": "Green"}, {"label": "Yellow"}]},
        ]
    })
    body = build_resolved_message(
        ev, "allow", "<@U1>",
        selected_options={"0": 0, "1": 1},  # Raccoon, Yellow
    )
    text = body["text"]
    # Header now uses "Answered" rather than "Approved" / "Selected `…`".
    assert "Answered by <@U1>" in text
    # The Q→A summary block contains both questions and both answers.
    section_texts = [
        b["text"]["text"]
        for b in body["attachments"][0]["blocks"]
        if b.get("type") == "section"
    ]
    full_text = "\n".join(section_texts)
    assert "Mascot?" in full_text and "Raccoon" in full_text
    assert "Color?" in full_text and "Yellow" in full_text


def test_permission_suggestions_render_extra_buttons():
    """When the PermissionRequest payload includes permission_suggestions,
    each one renders as an extra button below Approve/Deny on a non-
    AskUserQuestion tool. Tapping resolves the approval AND applies the
    rule edit via PermissionRequest's `decision.updatedPermissions`."""
    ev = _event(permission_suggestions=(
        {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "npm test"}],
         "behavior": "allow", "destination": "localSettings"},
        {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "*"}],
         "behavior": "allow", "destination": "localSettings"},
    ))
    body = build_approval_message(ev, "appr-sugg-1")
    actions_blocks = _find_blocks(body, "actions")
    # Two actions blocks: Approve/Deny + suggestions.
    assert len(actions_blocks) == 2
    approve_deny, suggestions = actions_blocks
    approve_deny_ids = [el["action_id"] for el in approve_deny["elements"]]
    assert approve_deny_ids == [APPROVE_ACTION_ID, DENY_ACTION_ID]
    suggestion_ids = [el["action_id"] for el in suggestions["elements"]]
    assert suggestion_ids == [
        f"{SUGGESTION_ACTION_ID_PREFIX}0",
        f"{SUGGESTION_ACTION_ID_PREFIX}1",
    ]
    # Labels surface the rule + destination so the user knows what tap
    # will actually do.
    assert "npm test" in suggestions["elements"][0]["text"]["text"]
    assert "localSettings" in suggestions["elements"][0]["text"]["text"]


def test_no_suggestion_buttons_without_suggestions():
    """No permission_suggestions → just the standard Approve/Deny block."""
    body = build_approval_message(_event(), "appr-no-sugg")
    actions_blocks = _find_blocks(body, "actions")
    assert len(actions_blocks) == 1
    assert [el["action_id"] for el in actions_blocks[0]["elements"]] == [
        APPROVE_ACTION_ID, DENY_ACTION_ID,
    ]


def test_no_suggestion_buttons_for_ask_user_question():
    """AskUserQuestion's option buttons ARE the answer; suggestion
    buttons would conflict, so we suppress them even if present in the
    payload."""
    ev = _ask_user_question_event(permission_suggestions=(
        {"type": "addRules", "rules": [{"toolName": "AskUserQuestion", "ruleContent": "*"}],
         "behavior": "allow", "destination": "localSettings"},
    ))
    body = build_approval_message(ev, "appr-aq-no-sugg")
    actions_blocks = _find_blocks(body, "actions")
    suggestion_ids = [
        el["action_id"]
        for block in actions_blocks
        for el in block["elements"]
        if el["action_id"].startswith(SUGGESTION_ACTION_ID_PREFIX)
    ]
    assert suggestion_ids == []


def test_build_resolved_message_uses_suggestion_label():
    """When a suggestion was clicked, the resolved message header tells
    the user what rule edit was applied."""
    body = build_resolved_message(
        _event(), "allow", "<@U1>",
        selected_suggestion_label="Approve & add `Bash(npm test)` to localSettings",
    )
    text = body["text"]
    assert "Approved & applied" in text
    assert "npm test" in text


def test_recommended_option_gets_primary_style():
    """An option label containing "(Recommended)" gets style:primary so
    Slack renders it as a filled green CTA — visual hint for the user's
    suggested pick."""
    ev = _ask_user_question_event(tool_input={
        "questions": [{
            "question": "Q?",
            "options": [
                {"label": "Global config only (Recommended)", "description": "..."},
                {"label": "Per-repo files", "description": "..."},
            ],
        }]
    })
    body = build_approval_message(ev, "appr-rec-1")
    actions = _find_block(body, "actions")
    # First option is recommended → primary style.
    assert actions["elements"][0].get("style") == "primary"
    # Second option has no special style.
    assert "style" not in actions["elements"][1]


def test_only_first_recommended_option_gets_primary():
    """If multiple options are tagged Recommended (uncommon but possible),
    only the first gets primary — Slack discourages multiple primary
    buttons in one actions block."""
    ev = _ask_user_question_event(tool_input={
        "questions": [{
            "question": "Q?",
            "options": [
                {"label": "First (Recommended)"},
                {"label": "Second (Recommended)"},
            ],
        }]
    })
    body = build_approval_message(ev, "appr-rec-2")
    actions = _find_block(body, "actions")
    assert actions["elements"][0].get("style") == "primary"
    assert "style" not in actions["elements"][1]


def test_no_primary_when_no_recommendation():
    """When no option is tagged Recommended, no option button gets primary
    style — they all render as outlined buttons."""
    body = build_approval_message(_ask_user_question_event(), "appr-no-rec")
    actions = _find_block(body, "actions")
    for el in actions["elements"][:-1]:  # all but the trailing Deny
        assert "style" not in el


def test_ask_user_question_uses_green_sidebar_and_thinking_emoji():
    """AskUserQuestion is a question, not a permission warning. Override
    the kind's yellow with green and the :pray: emoji with :thinking_face:
    so the visual reads as "question to answer" not "approve this risky
    thing"."""
    body = build_approval_message(_ask_user_question_event(), "appr-aq-vis")
    # Color overridden to green.
    assert body["attachments"][0]["color"] == "#2eb67d"
    # Header uses thinking emoji + "is asking" (not "needs approval").
    blocks = body["attachments"][0]["blocks"]
    header_text = blocks[0]["text"]["text"]
    assert ":thinking_face:" in header_text
    assert "is asking" in header_text
    assert "needs approval" not in header_text


def test_non_ask_user_question_keeps_kind_styling():
    """Bash and other tools still get the kind-based color (yellow for
    permission) and emoji (:pray: now)."""
    body = build_approval_message(_event(), "appr-bash-vis")  # Bash, kind=permission
    assert body["attachments"][0]["color"] == "#ecb22e"  # yellow
    header_text = body["attachments"][0]["blocks"][0]["text"]["text"]
    assert ":pray:" in header_text
    assert "needs approval" in header_text


def test_build_resolved_message_uses_selected_label_when_provided():
    body = build_resolved_message(
        _ask_user_question_event(),
        "allow",
        "<@U1>",
        selected_label="Per-repo files",
    )
    text_field = body["text"]
    assert "Selected `Per-repo files`" in text_field
    # Header block also reflects the selection.
    blocks = body["attachments"][0]["blocks"]
    header = blocks[0]["text"]["text"]
    assert "Selected `Per-repo files`" in header
    # Color stays the allow-green.
    assert body["attachments"][0]["color"] == "#2eb67d"


def test_build_resolved_message_without_selected_label_keeps_approved_wording():
    body = build_resolved_message(_event(), "allow", "<@U1>")
    assert "Approved" in body["text"]
    assert "Selected" not in body["text"]


def test_update_message_posts_to_chat_update():
    poster = _FakePoster(body={"ok": True, "channel": "C1", "ts": "1.0"})
    update_message("xoxb-test", "C1", "1.0", {"text": "hi"}, poster=poster)
    assert len(poster.calls) == 1
    assert poster.calls[0]["url"].endswith("/chat.update")
    assert poster.calls[0]["payload"]["channel"] == "C1"
    assert poster.calls[0]["payload"]["ts"] == "1.0"


def test_update_message_raises_on_api_error():
    poster = _FakePoster(body={"ok": False, "error": "message_not_found"})
    with pytest.raises(SinkError, match="message_not_found"):
        update_message("xoxb", "C1", "1.0", {}, poster=poster)


# --- helpers ---


def _find_block(body: dict, block_type: str) -> dict | None:
    attachments = body.get("attachments") or []
    for att in attachments:
        for b in att.get("blocks", []):
            if b.get("type") == block_type:
                return b
    for b in body.get("blocks", []):
        if b.get("type") == block_type:
            return b
    return None


def _find_blocks(body: dict, block_type: str) -> list[dict]:
    """Like _find_block but returns ALL matches in document order. Needed
    for multi-question AskUserQuestion which has one actions block per
    question plus a trailing Deny block."""
    out: list[dict] = []
    for att in body.get("attachments") or []:
        for b in att.get("blocks", []):
            if b.get("type") == block_type:
                out.append(b)
    for b in body.get("blocks", []):
        if b.get("type") == block_type:
            out.append(b)
    return out
