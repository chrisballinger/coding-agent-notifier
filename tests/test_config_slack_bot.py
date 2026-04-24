from __future__ import annotations

import pytest

from coding_agent_notifier.config import ConfigError, parse_config


def test_slack_bot_config_parsed_from_toml():
    cfg = parse_config({
        "sinks": {
            "slack": {
                "enabled": True,
                "bot_token": "xoxb-test",
                "app_token": "xapp-test",
                "channel": "@me",
                "interactive": True,
                "actionable_approvals": True,
                "approver_user_ids": ["U0123ABC", "U0456DEF"],
                "approval_timeout_seconds": 1200,
            },
        },
    })
    assert cfg.slack.interactive is True
    assert cfg.slack.actionable_approvals is True
    assert cfg.slack.approver_user_ids == ("U0123ABC", "U0456DEF")
    assert cfg.slack.approval_timeout_seconds == 1200.0
    assert cfg.slack.app_token == "xapp-test"


def test_interactive_requires_bot_token():
    with pytest.raises(ConfigError, match="interactive=true requires bot_token"):
        parse_config({
            "sinks": {
                "slack": {
                    "enabled": True,
                    "webhook_url": "https://hooks.slack.com/test",
                    "interactive": True,
                },
            },
        })


def test_actionable_approvals_requires_bot_token():
    with pytest.raises(ConfigError, match="actionable_approvals=true requires bot_token"):
        parse_config({
            "sinks": {
                "slack": {
                    "enabled": True,
                    "webhook_url": "https://hooks.slack.com/test",
                    "actionable_approvals": True,
                },
            },
        })


def test_actionable_approvals_requires_app_token():
    with pytest.raises(ConfigError, match="actionable_approvals=true requires app_token"):
        parse_config({
            "sinks": {
                "slack": {
                    "enabled": True,
                    "bot_token": "xoxb-test",
                    "actionable_approvals": True,
                },
            },
        })


def test_approval_timeout_must_be_positive():
    with pytest.raises(ConfigError, match="approval_timeout_seconds"):
        parse_config({
            "sinks": {
                "slack": {
                    "enabled": True,
                    "bot_token": "xoxb",
                    "approval_timeout_seconds": 0,
                },
            },
        })


def test_bot_token_env_resolution(monkeypatch):
    monkeypatch.setenv("MY_SLACK_BOT", "xoxb-from-env")
    cfg = parse_config({
        "sinks": {
            "slack": {
                "enabled": True,
                "bot_token_env": "MY_SLACK_BOT",
            },
        },
    })
    assert cfg.slack.bot_token == "xoxb-from-env"


def test_env_resolution_missing_var_leaves_none(monkeypatch):
    monkeypatch.delenv("NO_SUCH_VAR", raising=False)
    # enabled=False lets us parse without requiring a token. The _env key
    # resolves to None rather than raising at parse time.
    cfg = parse_config({
        "sinks": {
            "slack": {
                "enabled": False,
                "bot_token_env": "NO_SUCH_VAR",
            },
        },
    })
    assert cfg.slack.bot_token is None


def test_env_resolution_inline_wins_over_env(monkeypatch):
    monkeypatch.setenv("MY_SLACK_BOT", "xoxb-from-env")
    cfg = parse_config({
        "sinks": {
            "slack": {
                "enabled": True,
                "bot_token": "xoxb-inline",
                "bot_token_env": "MY_SLACK_BOT",
            },
        },
    })
    assert cfg.slack.bot_token == "xoxb-inline"


def test_approver_user_ids_must_be_strings():
    with pytest.raises(ConfigError, match="approver_user_ids"):
        parse_config({
            "sinks": {
                "slack": {
                    "enabled": True,
                    "bot_token": "xoxb",
                    "approver_user_ids": [123, 456],
                },
            },
        })


def test_approver_user_ids_must_be_array():
    with pytest.raises(ConfigError, match="approver_user_ids"):
        parse_config({
            "sinks": {
                "slack": {
                    "enabled": True,
                    "bot_token": "xoxb",
                    "approver_user_ids": "U123",
                },
            },
        })
