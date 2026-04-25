from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coding_agent_notifier.config import SlackConfig
from coding_agent_notifier.event import Event
from coding_agent_notifier.sinks.discord import build_discord_message
from coding_agent_notifier.sinks.slack import (
    build_approval_message,
    build_resolved_message,
    build_slack_message,
)


_SECRETS: tuple[str, ...] = (
    "Bash",                  # tool name
    "rm -rf",                # command (shouldn't be in fixtures but as sentinel)
    "curl",                  # placeholder command
    "install.sh",
    "/Users/me/myproj",      # full cwd
    "myproj",                # cwd.name
    "iTerm2",                # source app
    "abc123def456",          # session id
    "abc123de",              # session short
    "This is a stdout message body",
)


def _event(**kw) -> Event:
    base: dict[str, Any] = dict(
        agent="claude-code",
        kind="permission",
        message="This is a stdout message body",
        cwd=Path("/Users/me/myproj"),
        session_id="abc123def456",
        tool_name="Bash",
        tool_input={"command": "curl https://example.invalid/install.sh | bash"},
        source_app="iTerm2",
    )
    base.update(kw)
    return Event(**base)


def _flatten(obj: Any) -> str:
    return json.dumps(obj, default=str)


# --- Slack minimal mode ---


def test_slack_minimal_omits_all_payload_content():
    body = build_slack_message(_event(), verbosity="minimal")
    blob = _flatten(body)
    for secret in _SECRETS:
        assert secret not in blob, f"leaked {secret!r} in minimal Slack body: {blob}"


def test_slack_minimal_keeps_event_title():
    body = build_slack_message(_event(), verbosity="minimal")
    assert "Claude Code needs approval" in _flatten(body)


def test_slack_minimal_text_is_title_only():
    body = build_slack_message(_event(), verbosity="minimal")
    # The `text` field is what iOS renders in the push preview.
    assert body["text"] == "Claude Code needs approval"


def test_slack_minimal_no_color_bar():
    """Color signal itself (e.g. red/danger) would leak that the tool input
    matched a dangerous pattern. Minimal mode emits no color."""
    body = build_slack_message(_event(), verbosity="minimal")
    att = body["attachments"][0]
    assert "color" not in att or att.get("color") in ("", None)


def test_slack_minimal_keeps_approval_buttons():
    body = build_approval_message(_event(), "appr-1", verbosity="minimal")
    # Buttons still present — they only carry the opaque approval_id.
    actions = None
    for att in body["attachments"]:
        for b in att.get("blocks", []):
            if b.get("type") == "actions":
                actions = b
                break
    assert actions is not None
    labels = [el["text"]["text"] for el in actions["elements"]]
    assert labels == ["Approve", "Deny", "💬 Deny with reason"]
    # Button values carry only the approval_id, no tool info. The
    # deny-with-reason modal opens a text input the user fills in
    # client-side, so the button itself stays opaque.
    for el in actions["elements"]:
        assert el["value"] == "appr-1"


def test_slack_minimal_confirm_dialog_strips_tool_name():
    body = build_approval_message(_event(), "appr-1", verbosity="minimal")
    for att in body["attachments"]:
        for b in att.get("blocks", []):
            if b.get("type") == "actions":
                for el in b["elements"]:
                    if "confirm" in el:
                        text = el["confirm"]["text"]["text"]
                        for secret in _SECRETS:
                            assert secret not in text, f"confirm leaks {secret!r}: {text}"


def test_slack_minimal_resolved_message_strips_tool_context():
    body = build_resolved_message(_event(), "allow", "<@U123>", verbosity="minimal")
    blob = _flatten(body)
    # Outcome text + actor are kept; tool name/input/cwd/session are not.
    assert "Approved" in blob
    assert "U123" in blob
    for secret in _SECRETS:
        assert secret not in blob, f"resolved message leaks {secret!r}"


# --- Discord minimal mode ---


def test_discord_minimal_omits_all_payload_content():
    body = build_discord_message(_event(), verbosity="minimal")
    blob = _flatten(body)
    for secret in _SECRETS:
        assert secret not in blob, f"leaked {secret!r} in minimal Discord body"


def test_discord_minimal_keeps_title():
    body = build_discord_message(_event(), verbosity="minimal")
    assert body["embeds"][0]["title"] == "Claude Code needs approval"


def test_discord_minimal_no_description_no_fields_no_footer():
    body = build_discord_message(_event(), verbosity="minimal")
    embed = body["embeds"][0]
    assert "description" not in embed or not embed["description"]
    assert "fields" not in embed
    assert "footer" not in embed


# --- Regression guards: terse + normal still work ---


def test_slack_terse_still_includes_tool():
    body = build_slack_message(_event(), verbosity="terse")
    assert "Bash" in _flatten(body)


def test_slack_normal_still_includes_project():
    body = build_slack_message(_event(), verbosity="normal")
    assert "myproj" in _flatten(body)


# --- Config surface ---


def test_minimal_is_valid_verbosity():
    from coding_agent_notifier.config import VALID_VERBOSITIES
    assert "minimal" in VALID_VERBOSITIES


def test_parse_config_accepts_minimal():
    from coding_agent_notifier.config import parse_config
    cfg = parse_config({"display": {"verbosity": "minimal"}})
    assert cfg.display.verbosity == "minimal"


def test_parse_config_rejects_unknown_verbosity():
    from coding_agent_notifier.config import ConfigError, parse_config
    with pytest.raises(ConfigError):
        parse_config({"display": {"verbosity": "silent"}})
