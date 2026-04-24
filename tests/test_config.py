from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent_notifier import config as cfgmod


def test_defaults_when_file_missing(tmp_path: Path):
    c = cfgmod.load_config(tmp_path / "nope.toml")
    assert c.gating == "idle_or_background"
    assert c.slack.enabled is False
    assert c.event("permission").enabled is True
    assert c.tool_input_max_chars == 400


def test_tool_input_max_chars_override():
    c = cfgmod.parse_config({"tool_input_max_chars": 80})
    assert c.tool_input_max_chars == 80


def test_tool_input_max_chars_rejects_non_positive():
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.parse_config({"tool_input_max_chars": 0})


def test_full_parse(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text(
        """
idle_threshold_seconds = 30
gating = "idle_only"

[events.permission]
enabled = true
gating = "always"

[events.turn_complete]
enabled = false

[sinks.slack]
enabled = true
webhook_url = "https://hooks.slack.com/services/X/Y/Z"

[sinks.discord]
enabled = true
webhook_url = "https://discord.com/api/webhooks/1/2"
""".strip()
    )
    c = cfgmod.load_config(p)
    assert c.idle_threshold_seconds == 30
    assert c.gating == "idle_only"
    assert c.gating_for("permission") == "always"
    assert c.gating_for("idle_prompt") == "idle_only"
    assert c.event("turn_complete").enabled is False
    assert c.slack.enabled is True
    assert c.slack.webhook_url.startswith("https://hooks.slack.com")
    assert c.discord.enabled is True


def test_invalid_gating_rejected():
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.parse_config({"gating": "whenever"})


def test_unknown_event_kind_rejected():
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.parse_config({"events": {"bogus": {"enabled": True}}})


def test_event_entry_must_be_table():
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.parse_config({"events": {"permission": "yes"}})


def test_invalid_event_gating_rejected():
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.parse_config({"events": {"permission": {"gating": "nope"}}})


def test_slack_enabled_without_credentials_rejected():
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.parse_config({"sinks": {"slack": {"enabled": True}}})


def test_default_config_path_uses_xdg(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    p = cfgmod.default_config_path()
    assert str(p).startswith(str(tmp_path))
    assert p.name == "config.toml"


def test_default_config_path_fallback(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    p = cfgmod.default_config_path()
    assert ".config" in str(p)


def test_template_is_valid_toml():
    c = cfgmod.parse_config(_loads_toml(cfgmod.CONFIG_TEMPLATE))
    assert c.slack.enabled is True or c.slack.enabled is False  # template shape OK


def _loads_toml(text: str) -> dict:
    import tomllib
    return tomllib.loads(text)
