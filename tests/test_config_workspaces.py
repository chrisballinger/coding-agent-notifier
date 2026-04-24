from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_agent_notifier import config as cfgmod
from coding_agent_notifier import keychain


# ---------------------------------------------------------------------
# [slack.workspaces.<name>] shape
# ---------------------------------------------------------------------


def test_single_named_workspace_parses():
    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "home": {
                    "enabled": True,
                    "bot_token": "xoxb-home",
                    "app_token": "xapp-home",
                    "interactive": True,
                    "actionable_approvals": True,
                    "channel": "@me",
                    "approver_user_ids": ["U01"],
                },
            },
        },
    })
    assert "home" in cfg.slack_workspaces
    assert cfg.slack_workspaces["home"].bot_token == "xoxb-home"
    assert cfg.slack_workspaces["home"].interactive is True
    # No default was defined, so config.slack is the empty fallback.
    assert cfg.slack.bot_token is None
    assert cfg.slack.enabled is False


def test_multiple_workspaces_parse_independently():
    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "home": {"enabled": True, "bot_token": "xoxb-home"},
                "work": {"enabled": True, "bot_token": "xoxb-work", "channel": "#acme"},
            },
        },
    })
    assert set(cfg.slack_workspaces) == {"home", "work"}
    assert cfg.slack_workspaces["home"].bot_token == "xoxb-home"
    assert cfg.slack_workspaces["work"].channel == "#acme"


def test_legacy_sinks_slack_parses_as_default():
    cfg = cfgmod.parse_config({
        "sinks": {
            "slack": {
                "enabled": True,
                "bot_token": "xoxb-legacy",
            },
        },
    })
    # Back-compat: legacy block populates the "default" workspace…
    assert "default" in cfg.slack_workspaces
    assert cfg.slack_workspaces["default"].bot_token == "xoxb-legacy"
    # …and config.slack still points at it.
    assert cfg.slack.bot_token == "xoxb-legacy"


def test_both_legacy_and_new_default_is_error():
    with pytest.raises(cfgmod.ConfigError, match="both \\[sinks.slack\\] and \\[slack.workspaces.default\\]"):
        cfgmod.parse_config({
            "sinks": {"slack": {"enabled": True, "bot_token": "xoxb-a"}},
            "slack": {
                "workspaces": {
                    "default": {"enabled": True, "bot_token": "xoxb-b"},
                },
            },
        })


def test_new_default_and_named_workspaces_coexist():
    # Defining `default` explicitly in the new shape (with no legacy block) is fine.
    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "default": {"enabled": True, "bot_token": "xoxb-d"},
                "work": {"enabled": True, "bot_token": "xoxb-w"},
            },
        },
    })
    assert cfg.slack.bot_token == "xoxb-d"
    assert cfg.slack_workspaces["work"].bot_token == "xoxb-w"


def test_workspace_block_must_be_a_table():
    with pytest.raises(cfgmod.ConfigError, match="must be a table"):
        cfgmod.parse_config({
            "slack": {"workspaces": {"home": "not-a-table"}},
        })


def test_slack_workspaces_must_be_a_table():
    with pytest.raises(cfgmod.ConfigError, match="slack.workspaces must be a table"):
        cfgmod.parse_config({"slack": {"workspaces": "nope"}})


def test_slack_top_must_be_a_table():
    with pytest.raises(cfgmod.ConfigError, match="slack must be a table"):
        cfgmod.parse_config({"slack": "nope"})


def test_empty_workspace_name_rejected():
    # tomllib won't normally produce an empty key, but parse_config is called
    # directly with dicts in tests and from the CLI. Be defensive.
    with pytest.raises(cfgmod.ConfigError, match="non-empty"):
        cfgmod.parse_config({
            "slack": {"workspaces": {"": {"enabled": True, "bot_token": "x"}}},
        })


def test_workspace_inherits_same_validation_as_legacy():
    # actionable_approvals=true in a named workspace must still require both tokens.
    with pytest.raises(cfgmod.ConfigError, match="actionable_approvals=true requires app_token"):
        cfgmod.parse_config({
            "slack": {
                "workspaces": {
                    "home": {
                        "enabled": True,
                        "bot_token": "xoxb-home",
                        "actionable_approvals": True,
                    },
                },
            },
        })


# ---------------------------------------------------------------------
# Route → workspace references
# ---------------------------------------------------------------------


def test_route_can_reference_workspace_by_name(tmp_path):
    # Use a real tmp_path to avoid /tmp symlink resolution (→ /private/tmp) on macOS.
    (tmp_path / "acme-foo").mkdir()
    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "home": {"enabled": True, "bot_token": "xoxb-home"},
                "work": {"enabled": True, "bot_token": "xoxb-work", "channel": "#work"},
            },
        },
        "routes": [
            {"cwd": f"{tmp_path}/acme-*", "slack": {"workspace": "work"}},
        ],
    })
    resolved = cfgmod.sinks_for(tmp_path / "acme-foo", cfg)
    assert resolved is not None
    slack_cfg, _ = resolved
    assert slack_cfg.bot_token == "xoxb-work"
    assert slack_cfg.channel == "#work"


def test_route_workspace_plus_field_override(tmp_path):
    (tmp_path / "acme-foo").mkdir()
    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "work": {"enabled": True, "bot_token": "xoxb-work", "channel": "#default"},
            },
        },
        "routes": [
            {
                "cwd": f"{tmp_path}/acme-*",
                "slack": {"workspace": "work", "channel": "#per-route"},
            },
        ],
    })
    resolved = cfgmod.sinks_for(tmp_path / "acme-foo", cfg)
    assert resolved is not None
    slack_cfg, _ = resolved
    # workspace = base; channel override patches on top.
    assert slack_cfg.bot_token == "xoxb-work"
    assert slack_cfg.channel == "#per-route"


def test_route_references_unknown_workspace_is_error():
    with pytest.raises(cfgmod.ConfigError, match="refs an undefined workspace"):
        cfgmod.parse_config({
            "slack": {
                "workspaces": {"home": {"enabled": True, "bot_token": "x"}},
            },
            "routes": [
                {"cwd": "/x/*", "slack": {"workspace": "ghost"}},
            ],
        })


def test_route_workspace_must_be_nonempty_string():
    with pytest.raises(cfgmod.ConfigError, match="must be a non-empty string"):
        cfgmod.parse_config({
            "slack": {"workspaces": {"home": {"enabled": True, "bot_token": "x"}}},
            "routes": [{"cwd": "/x/*", "slack": {"workspace": ""}}],
        })


def test_route_workspace_is_known_override_key():
    # Sanity: _VALID_SLACK_OVERRIDE_KEYS should accept "workspace".
    cfg = cfgmod.parse_config({
        "slack": {"workspaces": {"home": {"enabled": True, "bot_token": "x"}}},
        "routes": [{"cwd": "/x/*", "slack": {"workspace": "home"}}],
    })
    assert cfg.routes[0].slack == {"workspace": "home"}


def test_strict_routing_still_applies():
    # Adding workspaces doesn't change strict-routing behavior: unmatched cwd → None.
    cfg = cfgmod.parse_config({
        "slack": {"workspaces": {"home": {"enabled": True, "bot_token": "x"}}},
        "routes": [{"cwd": "/work/*", "slack": {"workspace": "home"}}],
    })
    assert cfgmod.sinks_for(Path("/unrouted/repo"), cfg) is None


# ---------------------------------------------------------------------
# Keychain resolution path (_resolve_secret)
# ---------------------------------------------------------------------


def test_keychain_resolves_when_account_exists(monkeypatch):
    monkeypatch.setattr(keychain, "read", lambda account: "xoxb-from-keychain")
    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "home": {
                    "enabled": True,
                    "bot_token_keychain": "home:bot_token",
                },
            },
        },
    })
    assert cfg.slack_workspaces["home"].bot_token == "xoxb-from-keychain"


def test_keychain_missing_account_raises_configerror(monkeypatch):
    monkeypatch.setattr(keychain, "read", lambda account: None)
    with pytest.raises(cfgmod.ConfigError, match="no entry in macOS Keychain"):
        cfgmod.parse_config({
            "slack": {
                "workspaces": {
                    "home": {
                        "enabled": True,
                        "bot_token_keychain": "home:bot_token",
                    },
                },
            },
        })


def test_keychain_subprocess_failure_raises_configerror(monkeypatch):
    def boom(account):
        raise keychain.KeychainError("security timed out")

    monkeypatch.setattr(keychain, "read", boom)
    with pytest.raises(cfgmod.ConfigError, match="Keychain read failed"):
        cfgmod.parse_config({
            "slack": {
                "workspaces": {
                    "home": {
                        "enabled": True,
                        "bot_token_keychain": "home:bot_token",
                    },
                },
            },
        })


def test_inline_wins_over_keychain(monkeypatch):
    # Should never actually call keychain.read if inline is set.
    def nope(_a):
        pytest.fail("keychain.read should not be called when inline value present")

    monkeypatch.setattr(keychain, "read", nope)
    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "home": {
                    "enabled": True,
                    "bot_token": "xoxb-inline",
                    "bot_token_keychain": "home:bot_token",
                },
            },
        },
    })
    assert cfg.slack_workspaces["home"].bot_token == "xoxb-inline"


def test_env_wins_over_keychain(monkeypatch):
    monkeypatch.setenv("MY_BOT", "xoxb-env")

    def nope(_a):
        pytest.fail("keychain.read should not be called when env var present")

    monkeypatch.setattr(keychain, "read", nope)
    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "home": {
                    "enabled": True,
                    "bot_token_env": "MY_BOT",
                    "bot_token_keychain": "home:bot_token",
                },
            },
        },
    })
    assert cfg.slack_workspaces["home"].bot_token == "xoxb-env"


# ---------------------------------------------------------------------
# secrets.toml
# ---------------------------------------------------------------------


def _write_strict(path: Path, text: str) -> None:
    path.write_text(text)
    os.chmod(path, 0o600)


def test_secrets_toml_fills_missing_bot_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    secrets_path = tmp_path / "secrets.toml"
    _write_strict(config_path, """
[slack.workspaces.home]
enabled = true
# bot_token lives in secrets.toml
""".strip())
    _write_strict(secrets_path, """
[slack.workspaces.home]
bot_token = "xoxb-from-secrets"
""".strip())
    cfg = cfgmod.load_config()
    assert cfg.slack_workspaces["home"].bot_token == "xoxb-from-secrets"


def test_config_toml_wins_over_secrets_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    secrets_path = tmp_path / "secrets.toml"
    _write_strict(config_path, """
[slack.workspaces.home]
enabled = true
bot_token = "xoxb-config"
""".strip())
    _write_strict(secrets_path, """
[slack.workspaces.home]
bot_token = "xoxb-secrets"
""".strip())
    cfg = cfgmod.load_config()
    assert cfg.slack_workspaces["home"].bot_token == "xoxb-config"


def test_secrets_toml_refuses_loose_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    secrets_path = tmp_path / "secrets.toml"
    _write_strict(config_path, "[slack.workspaces.home]\nenabled = true\n")
    secrets_path.write_text("[slack.workspaces.home]\nbot_token = \"x\"\n")
    os.chmod(secrets_path, 0o644)  # group/world readable
    with pytest.raises(cfgmod.ConfigError, match="must be owner-only"):
        cfgmod.load_config()


def test_secrets_toml_missing_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    _write_strict(config_path, """
[slack.workspaces.home]
enabled = true
bot_token = "xoxb-inline"
""".strip())
    # No secrets.toml — load_config should not raise.
    cfg = cfgmod.load_config()
    assert cfg.slack_workspaces["home"].bot_token == "xoxb-inline"


def test_secrets_toml_merges_nested_tables(tmp_path, monkeypatch):
    # Multiple workspaces, secrets.toml adds tokens to each without clobbering
    # the non-secret fields from config.toml.
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    secrets_path = tmp_path / "secrets.toml"
    _write_strict(config_path, """
[slack.workspaces.home]
enabled = true
channel = "@me"

[slack.workspaces.work]
enabled = true
channel = "#work"
""".strip())
    _write_strict(secrets_path, """
[slack.workspaces.home]
bot_token = "xoxb-home"

[slack.workspaces.work]
bot_token = "xoxb-work"
""".strip())
    cfg = cfgmod.load_config()
    assert cfg.slack_workspaces["home"].channel == "@me"
    assert cfg.slack_workspaces["home"].bot_token == "xoxb-home"
    assert cfg.slack_workspaces["work"].channel == "#work"
    assert cfg.slack_workspaces["work"].bot_token == "xoxb-work"


def test_secrets_toml_not_loaded_when_config_toml_absent(tmp_path, monkeypatch):
    # An unattached secrets.toml (no config.toml) returns the default empty
    # Config — we don't try to read the secrets file in that case.
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    secrets_path = tmp_path / "secrets.toml"
    # Write it loose on purpose — if load_config touched it, it'd raise.
    secrets_path.write_text("[slack.workspaces.home]\nbot_token = \"x\"\n")
    os.chmod(secrets_path, 0o644)
    cfg = cfgmod.load_config()
    assert cfg.slack.enabled is False


# ---------------------------------------------------------------------
# Authorization: approver allowlists
# ---------------------------------------------------------------------


def test_actionable_approvals_requires_allowlist_for_non_dm_channel():
    # Empty allowlist is a footgun for any shared channel → reject.
    with pytest.raises(cfgmod.ConfigError, match="requires a non-empty"):
        cfgmod.parse_config({
            "sinks": {
                "slack": {
                    "enabled": True,
                    "bot_token": "xoxb-x",
                    "app_token": "xapp-x",
                    "actionable_approvals": True,
                    "channel": "#acme",
                    # no approver_user_ids, no approver_user_groups
                },
            },
        })


def test_actionable_approvals_allows_empty_allowlist_when_channel_is_dm():
    # @me = DM with the bot. Only the installing user can see the message,
    # so an explicit allowlist is redundant. Runtime enforces this by
    # double-checking the channel_id at click time.
    cfg = cfgmod.parse_config({
        "sinks": {
            "slack": {
                "enabled": True,
                "bot_token": "xoxb-x",
                "app_token": "xapp-x",
                "actionable_approvals": True,
                "channel": "@me",
            },
        },
    })
    assert cfg.slack.channel == "@me"
    assert cfg.slack.approver_user_ids == ()


def test_actionable_approvals_default_channel_is_dm_friendly():
    # No channel explicitly set → defaults to @me at validation time, so
    # empty allowlist is allowed (zero-config easy setup).
    cfg = cfgmod.parse_config({
        "sinks": {
            "slack": {
                "enabled": True,
                "bot_token": "xoxb-x",
                "app_token": "xapp-x",
                "actionable_approvals": True,
            },
        },
    })
    # channel attribute stays None (the `@me` treatment is applied by
    # downstream senders); what matters is parse-time acceptance.
    assert cfg.slack.channel is None


def test_actionable_approvals_accepts_user_groups_alone():
    # Having only usergroups (no individual IDs) is allowed — common for
    # "anyone in @oncall subteam can approve" setups.
    cfg = cfgmod.parse_config({
        "sinks": {
            "slack": {
                "enabled": True,
                "bot_token": "xoxb-x",
                "app_token": "xapp-x",
                "actionable_approvals": True,
                "approver_user_groups": ["S01ABCDEF"],
            },
        },
    })
    assert cfg.slack.approver_user_groups == ("S01ABCDEF",)
    assert cfg.slack.approver_user_ids == ()


def test_actionable_approvals_accepts_user_ids_alone():
    cfg = cfgmod.parse_config({
        "sinks": {
            "slack": {
                "enabled": True,
                "bot_token": "xoxb-x",
                "app_token": "xapp-x",
                "actionable_approvals": True,
                "approver_user_ids": ["U01"],
            },
        },
    })
    assert cfg.slack.approver_user_ids == ("U01",)


def test_actionable_approvals_accepts_both_lists():
    cfg = cfgmod.parse_config({
        "sinks": {
            "slack": {
                "enabled": True,
                "bot_token": "xoxb-x",
                "app_token": "xapp-x",
                "actionable_approvals": True,
                "approver_user_ids": ["U01"],
                "approver_user_groups": ["S01"],
            },
        },
    })
    assert cfg.slack.approver_user_ids == ("U01",)
    assert cfg.slack.approver_user_groups == ("S01",)


def test_approver_user_groups_must_be_list_of_strings():
    with pytest.raises(cfgmod.ConfigError, match="approver_user_groups"):
        cfgmod.parse_config({
            "sinks": {
                "slack": {
                    "enabled": True,
                    "bot_token": "xoxb",
                    "approver_user_groups": [123],
                },
            },
        })


def test_approver_user_groups_is_valid_route_override(tmp_path):
    # Route can override approver_user_groups alongside other fields.
    (tmp_path / "acme").mkdir()
    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "work": {
                    "enabled": True,
                    "bot_token": "xoxb",
                    "app_token": "xapp",
                    "actionable_approvals": True,
                    "approver_user_ids": ["U01"],
                },
            },
        },
        "routes": [
            {
                "cwd": f"{tmp_path}/acme",
                "slack": {
                    "workspace": "work",
                    "approver_user_groups": ["S01"],
                },
            },
        ],
    })
    resolved = cfgmod.sinks_for(tmp_path / "acme", cfg)
    assert resolved is not None
    slack_cfg, _ = resolved
    assert slack_cfg.approver_user_groups == ("S01",)


def test_actionable_approvals_in_named_workspace_rejects_shared_channel_without_allowlist():
    # Same rule applies to named workspaces, not just legacy [sinks.slack].
    with pytest.raises(cfgmod.ConfigError, match="requires a non-empty"):
        cfgmod.parse_config({
            "slack": {
                "workspaces": {
                    "work": {
                        "enabled": True,
                        "bot_token": "xoxb-w",
                        "app_token": "xapp-w",
                        "actionable_approvals": True,
                        "channel": "#work",
                    },
                },
            },
        })


def test_merge_secrets_preserves_base_scalars_over_secret_tables():
    # Defensive: if the user accidentally has a scalar in config where secrets
    # has a table (weird shape mismatch), we keep the base and skip the secret
    # silently rather than blow up.
    merged = cfgmod._merge_secrets(
        {"x": "keep"},
        {"x": {"y": 1}},
    )
    assert merged == {"x": "keep"}
