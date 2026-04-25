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
