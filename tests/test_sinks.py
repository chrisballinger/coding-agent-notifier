from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coding_agent_notifier.config import DiscordConfig, SlackConfig
from coding_agent_notifier.event import Event
from coding_agent_notifier.sinks import base as sink_base
from coding_agent_notifier.sinks import slack as slack_mod
from coding_agent_notifier.sinks.discord import (
    DiscordSink,
    build_discord_message,
    _KIND_COLORS as DISCORD_COLORS,
    _DANGER_COLOR as DISCORD_DANGER,
)
from coding_agent_notifier.sinks.slack import (
    SlackSink,
    _KIND_COLORS as SLACK_COLORS,
    _DANGER_COLOR as SLACK_DANGER,
    build_slack_message,
    resolve_self_channel,
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


# --- Slack payload shape ---


def test_slack_message_wraps_blocks_in_colored_attachment():
    body = build_slack_message(_event())
    assert "attachments" in body
    assert len(body["attachments"]) == 1
    att = body["attachments"][0]
    assert att["color"] == SLACK_COLORS["permission"]
    kinds = [b["type"] for b in att["blocks"]]
    assert kinds[0] == "header"
    assert "section" in kinds


def test_slack_message_includes_tool_input_codeblock():
    body = build_slack_message(_event())
    joined = json.dumps(body)
    assert "echo hi" in joined
    assert "```" in joined


def test_slack_message_omits_session_when_none():
    body = build_slack_message(_event(session_id=None))
    joined = json.dumps(body)
    assert "abc123" not in joined


def test_slack_message_turn_complete_color():
    body = build_slack_message(_event(kind="turn_complete", tool_name=None, tool_input=None, message="done"))
    assert body["attachments"][0]["color"] == SLACK_COLORS["turn_complete"]


def test_slack_message_dangerous_command_highlight():
    body = build_slack_message(_event(tool_input={"command": "sudo rm -rf /"}))
    att = body["attachments"][0]
    assert att["color"] == SLACK_DANGER
    header = att["blocks"][0]["text"]["text"]
    assert ":rotating_light:" in header
    assert "DANGEROUS" in body["text"]


def test_slack_message_polishes_paths_to_inline_code():
    body = build_slack_message(
        _event(tool_name=None, tool_input=None, message="edit /Users/me/file.py please")
    )
    found = json.dumps(body)
    assert "`/Users/me/file.py`" in found


def test_slack_message_linkifies_urls():
    body = build_slack_message(
        _event(tool_name=None, tool_input=None, message="see https://docs.example.com/x for details")
    )
    found = json.dumps(body)
    assert "<https://docs.example.com/x|docs.example.com>" in found


# --- Slack iOS fallback text ---


def test_slack_fallback_text_is_single_line_and_capped():
    """The `text` field is what iOS renders in the push preview. Avoid
    duplication / wall of text by collapsing newlines and capping length."""
    long_body = "word " * 200  # 1000+ chars across many spaces
    body = build_slack_message(
        _event(kind="turn_complete", tool_name=None, tool_input=None, message=long_body)
    )
    text = body["text"]
    assert "\n" not in text
    # Allow a little over 140 for the "Claude Code ... — " prefix
    assert len(text) <= 200
    assert "…" in text  # truncation marker


def test_slack_fallback_collapses_multiline_body():
    body = build_slack_message(
        _event(kind="turn_complete", tool_name=None, tool_input=None,
               message="line one\n…\nline two")
    )
    text = body["text"]
    assert "\n" not in text
    assert "line one" in text


# --- Slack terse mode ---


def test_slack_terse_drops_fields_block_and_adds_footer():
    body = build_slack_message(_event(), verbosity="terse")
    att = body["attachments"][0]
    types = [b["type"] for b in att["blocks"]]
    # header, body section, detail section, footer context — no 4-field block
    assert "context" in types
    # Confirm no block is a section with a `fields` array (the 4-field layout)
    assert not any(b.get("fields") for b in att["blocks"])
    footer = next(b for b in att["blocks"] if b["type"] == "context")
    footer_text = footer["elements"][0]["text"]
    assert "myproj" in footer_text
    assert "abc123de" in footer_text  # session[:8]
    assert "iTerm2" in footer_text


def test_slack_terse_inlines_tool_name_in_body():
    body = build_slack_message(_event(), verbosity="terse")
    att = body["attachments"][0]
    body_sections = [b for b in att["blocks"] if b.get("type") == "section"]
    body_text = "\n".join(b["text"]["text"] for b in body_sections if "text" in b)
    assert "*Bash:*" in body_text


def test_slack_terse_footer_omits_missing_bits():
    body = build_slack_message(
        _event(session_id=None, source_app=None), verbosity="terse"
    )
    att = body["attachments"][0]
    contexts = [b for b in att["blocks"] if b["type"] == "context"]
    # Still has a footer with the project bit
    text = contexts[0]["elements"][0]["text"]
    assert "myproj" in text
    assert "·" not in text or text.count("·") == 0  # only project, no separators


def test_slack_normal_mode_still_has_fields_block():
    body = build_slack_message(_event(), verbosity="normal")
    att = body["attachments"][0]
    assert any(b.get("fields") for b in att["blocks"])
    assert not any(b.get("type") == "context" for b in att["blocks"])


# --- Slack sink dispatch ---


class _FakePoster:
    def __init__(self, responses):
        self._queue = list(responses)
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(self, url, body, *, headers=None, timeout=10.0):
        self.calls.append((url, body, dict(headers or {})))
        if not self._queue:
            return 200, "ok"
        return self._queue.pop(0)


@pytest.fixture
def fake_post(monkeypatch):
    def _setup(module, responses):
        poster = _FakePoster(responses)
        monkeypatch.setattr(module, "http_post_json", poster)
        return poster
    return _setup


def test_slack_webhook_happy_path(fake_post):
    poster = fake_post(slack_mod, [(200, "ok")])
    sink = SlackSink(SlackConfig(enabled=True, webhook_url="https://hook.test/x"))
    sink.send(_event())
    assert len(poster.calls) == 1
    assert poster.calls[0][0] == "https://hook.test/x"


def test_slack_webhook_non_ok_body_raises(fake_post):
    fake_post(slack_mod, [(200, "invalid_token")])
    sink = SlackSink(SlackConfig(enabled=True, webhook_url="https://hook.test/x"))
    with pytest.raises(sink_base.SinkError):
        sink.send(_event())


def test_slack_webhook_http_error_raises(fake_post):
    fake_post(slack_mod, [(500, "boom")])
    sink = SlackSink(SlackConfig(enabled=True, webhook_url="https://hook.test/x"))
    with pytest.raises(sink_base.SinkError):
        sink.send(_event())


def test_slack_bot_token_resolves_self_and_posts(fake_post):
    poster = fake_post(
        slack_mod,
        [
            (200, json.dumps({"ok": True, "user_id": "U1"})),
            (200, json.dumps({"ok": True})),
        ],
    )
    sink = SlackSink(SlackConfig(enabled=True, bot_token="xoxb-abc"))
    sink.send(_event())
    assert len(poster.calls) == 2
    assert poster.calls[0][0].endswith("auth.test")
    assert poster.calls[1][0].endswith("chat.postMessage")
    assert poster.calls[1][1]["channel"] == "U1"
    assert poster.calls[1][2]["Authorization"].startswith("Bearer ")


def test_slack_bot_token_explicit_channel_skips_auth_test(fake_post):
    poster = fake_post(slack_mod, [(200, json.dumps({"ok": True}))])
    sink = SlackSink(SlackConfig(enabled=True, bot_token="xoxb-abc", channel="C123"))
    sink.send(_event())
    assert len(poster.calls) == 1
    assert poster.calls[0][1]["channel"] == "C123"


def test_slack_bot_token_api_error(fake_post):
    fake_post(
        slack_mod,
        [(200, json.dumps({"ok": False, "error": "channel_not_found"}))],
    )
    sink = SlackSink(SlackConfig(enabled=True, bot_token="xoxb-abc", channel="C123"))
    with pytest.raises(sink_base.SinkError):
        sink.send(_event())


def test_slack_no_creds_raises(fake_post):
    fake_post(slack_mod, [])
    sink = SlackSink(SlackConfig(enabled=True))
    with pytest.raises(sink_base.SinkError):
        sink.send(_event())


def test_slack_disabled_is_noop(fake_post):
    poster = fake_post(slack_mod, [])
    sink = SlackSink(SlackConfig(enabled=False, webhook_url="https://x.test"))
    sink.send(_event())
    assert poster.calls == []


def test_resolve_self_channel_http_error(fake_post):
    fake_post(slack_mod, [(500, "nope")])
    with pytest.raises(sink_base.SinkError):
        resolve_self_channel("xoxb-abc")


# --- Discord sink ---


def test_discord_message_shape_and_color():
    body = build_discord_message(_event())
    embed = body["embeds"][0]
    assert embed["title"].startswith("Claude Code")
    assert embed["color"] == DISCORD_COLORS["permission"]
    names = [f["name"] for f in embed["fields"]]
    assert "Project" in names
    assert "echo hi" in embed["description"]


def test_discord_message_dangerous_highlight():
    body = build_discord_message(_event(tool_input={"command": "git push --force"}))
    embed = body["embeds"][0]
    assert embed["color"] == DISCORD_DANGER
    assert embed["title"].startswith("🚨")


def test_discord_sink_posts(fake_post):
    mod = __import__("coding_agent_notifier.sinks.discord", fromlist=["x"])
    poster = fake_post(mod, [(200, "")])
    sink = DiscordSink(DiscordConfig(enabled=True, webhook_url="https://discord.test/h"))
    sink.send(_event())
    assert len(poster.calls) == 1


def test_discord_sink_disabled(fake_post):
    mod = __import__("coding_agent_notifier.sinks.discord", fromlist=["x"])
    poster = fake_post(mod, [])
    DiscordSink(DiscordConfig(enabled=False)).send(_event())
    assert poster.calls == []


def test_discord_sink_requires_webhook():
    sink = DiscordSink(DiscordConfig(enabled=True, webhook_url=None))
    with pytest.raises(sink_base.SinkError):
        sink.send(_event())


def test_discord_terse_drops_fields_uses_footer():
    body = build_discord_message(_event(), verbosity="terse")
    embed = body["embeds"][0]
    assert "fields" not in embed
    assert embed["footer"]["text"].startswith("myproj")
    assert "abc123de" in embed["footer"]["text"]
    assert "**Bash:**" in embed["description"]


def test_discord_normal_keeps_fields_no_footer():
    body = build_discord_message(_event(), verbosity="normal")
    embed = body["embeds"][0]
    assert "fields" in embed
    assert "footer" not in embed


def test_discord_sink_http_failure(fake_post):
    mod = __import__("coding_agent_notifier.sinks.discord", fromlist=["x"])
    fake_post(mod, [(500, "boom")])
    sink = DiscordSink(DiscordConfig(enabled=True, webhook_url="https://discord.test/h"))
    with pytest.raises(sink_base.SinkError):
        sink.send(_event())


# --- base helper ---


def test_http_post_json_urlerror_maps_to_sink_error(monkeypatch):
    from urllib.error import URLError

    def _raise(*_a, **_kw):
        raise URLError("boom")

    monkeypatch.setattr(sink_base._urlreq, "urlopen", _raise)
    with pytest.raises(sink_base.SinkError):
        sink_base.http_post_json("https://example.test/x", {"a": 1})
