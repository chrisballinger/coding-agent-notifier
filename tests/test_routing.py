from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent_notifier import config as cfgmod
from coding_agent_notifier.config import (
    Config,
    DiscordConfig,
    Route,
    SlackConfig,
    match_route,
    parse_config,
    sinks_for,
)


# --- parse_config ---


def test_parses_routes_array():
    raw = {
        "sinks": {"slack": {"enabled": True, "webhook_url": "https://base.test/x"}},
        "routes": [
            {"cwd": "~/work/*", "slack": {"webhook_url": "https://work.test/y"}},
            {"cwd": "~/personal/*", "slack": {"channel": "#me"}},
        ],
    }
    c = parse_config(raw)
    assert len(c.routes) == 2
    assert c.routes[0].cwd == "~/work/*"
    assert c.routes[0].slack == {"webhook_url": "https://work.test/y"}
    assert c.routes[1].slack == {"channel": "#me"}


def test_rejects_non_list_routes():
    with pytest.raises(cfgmod.ConfigError):
        parse_config({"routes": "not a list"})


def test_rejects_route_without_cwd():
    with pytest.raises(cfgmod.ConfigError):
        parse_config({"routes": [{"slack": {"channel": "#c"}}]})


def test_rejects_route_with_empty_cwd():
    with pytest.raises(cfgmod.ConfigError):
        parse_config({"routes": [{"cwd": "   "}]})


def test_rejects_route_non_dict_entry():
    with pytest.raises(cfgmod.ConfigError):
        parse_config({"routes": ["~/foo"]})


def test_rejects_non_table_slack_override():
    with pytest.raises(cfgmod.ConfigError):
        parse_config({"routes": [{"cwd": "~/x", "slack": "not a table"}]})


def test_rejects_unknown_slack_override_key():
    with pytest.raises(cfgmod.ConfigError):
        parse_config({"routes": [{"cwd": "~/x", "slack": {"token": "xoxb-"}}]})


def test_rejects_unknown_discord_override_key():
    with pytest.raises(cfgmod.ConfigError):
        parse_config({"routes": [{"cwd": "~/x", "discord": {"foo": 1}}]})


def test_rejects_non_table_discord_override():
    with pytest.raises(cfgmod.ConfigError):
        parse_config({"routes": [{"cwd": "~/x", "discord": "bad"}]})


# --- match_route ---


def _cfg_with(routes: list[Route]) -> Config:
    return Config(routes=tuple(routes))


def test_match_returns_first_match(tmp_path: Path):
    cfg = _cfg_with([
        Route(cwd=str(tmp_path / "work" / "*"), slack={"channel": "#w"}),
        Route(cwd=str(tmp_path / "**"), slack={"channel": "#catchall"}),
    ])
    (tmp_path / "work" / "acme").mkdir(parents=True)
    assert match_route(tmp_path / "work" / "acme", cfg).slack == {"channel": "#w"}


def test_match_none_when_no_pattern_hits(tmp_path: Path):
    cfg = _cfg_with([Route(cwd=str(tmp_path / "other" / "*"))])
    (tmp_path / "work").mkdir()
    assert match_route(tmp_path / "work", cfg) is None


def test_match_expands_tilde(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "personal" / "notes").mkdir(parents=True)
    cfg = _cfg_with([Route(cwd="~/personal/*", slack={"channel": "#p"})])
    assert match_route(Path("~/personal/notes"), cfg) is not None


def test_match_gracefully_handles_unresolvable_path(tmp_path: Path):
    cfg = _cfg_with([Route(cwd="/nonexistent/*")])
    # A path that doesn't exist should still match by string (no resolve error).
    p = Path("/nonexistent/thing")
    assert match_route(p, cfg) is not None


# --- sinks_for ---


def test_sinks_for_no_routes_returns_base():
    cfg = Config(slack=SlackConfig(enabled=True, webhook_url="https://base/x"))
    result = sinks_for(Path("/tmp"), cfg)
    assert result is not None
    slack, discord = result
    assert slack.webhook_url == "https://base/x"
    assert discord == DiscordConfig()


def test_sinks_for_returns_none_when_routes_defined_and_none_match(tmp_path: Path):
    """Strict routing: the whole point of routes is to prevent cross-project
    leakage. If a path doesn't match any route, we must skip dispatch rather
    than silently fall back to the global sink."""
    cfg = Config(
        slack=SlackConfig(enabled=True, webhook_url="https://base/x"),
        routes=(Route(cwd=str(tmp_path / "work" / "*"), slack={"channel": "#w"}),),
    )
    (tmp_path / "unrelated").mkdir()
    assert sinks_for(tmp_path / "unrelated", cfg) is None


def test_sinks_for_merges_slack_override(tmp_path: Path):
    cfg = Config(
        slack=SlackConfig(enabled=True, webhook_url="https://base/x"),
        routes=(Route(cwd=str(tmp_path / "**"), slack={"webhook_url": "https://repo/y"}),),
    )
    result = sinks_for(tmp_path / "sub", cfg)
    assert result is not None
    slack, _ = result
    assert slack.webhook_url == "https://repo/y"
    assert slack.enabled is True


def test_sinks_for_can_disable_per_route(tmp_path: Path):
    cfg = Config(
        slack=SlackConfig(enabled=True, webhook_url="https://base/x"),
        routes=(Route(cwd=str(tmp_path / "quiet" / "*"), slack={"enabled": False}),),
    )
    (tmp_path / "quiet" / "x").mkdir(parents=True)
    result = sinks_for(tmp_path / "quiet" / "x", cfg)
    assert result is not None
    slack, _ = result
    assert slack.enabled is False


def test_sinks_for_swaps_bot_channel(tmp_path: Path):
    cfg = Config(
        slack=SlackConfig(enabled=True, bot_token="xoxb-", channel="@me"),
        routes=(Route(cwd=str(tmp_path / "**"), slack={"channel": "#work"}),),
    )
    result = sinks_for(tmp_path / "proj", cfg)
    assert result is not None
    slack, _ = result
    assert slack.channel == "#work"
    assert slack.bot_token == "xoxb-"


def test_sinks_for_overrides_discord(tmp_path: Path):
    cfg = Config(
        discord=DiscordConfig(enabled=False, webhook_url=None),
        routes=(Route(
            cwd=str(tmp_path / "**"),
            discord={"enabled": True, "webhook_url": "https://d/h"},
        ),),
    )
    result = sinks_for(tmp_path / "a", cfg)
    assert result is not None
    _, discord = result
    assert discord.enabled is True
    assert discord.webhook_url == "https://d/h"


def test_sinks_for_first_match_wins(tmp_path: Path):
    cfg = Config(
        slack=SlackConfig(enabled=True, channel="base"),
        routes=(
            Route(cwd=str(tmp_path / "a" / "*"), slack={"channel": "first"}),
            Route(cwd=str(tmp_path / "**"), slack={"channel": "second"}),
        ),
    )
    (tmp_path / "a" / "x").mkdir(parents=True)
    result = sinks_for(tmp_path / "a" / "x", cfg)
    assert result is not None
    slack, _ = result
    assert slack.channel == "first"


def test_catchall_route_re_enables_fallback_explicitly(tmp_path: Path):
    """Users who want a default sink alongside project-specific routes can
    express it explicitly with a `cwd = "*"` catch-all route."""
    cfg = Config(
        slack=SlackConfig(enabled=True, webhook_url="https://base/x"),
        routes=(
            Route(cwd=str(tmp_path / "work" / "*"), slack={"webhook_url": "https://work/y"}),
            Route(cwd="*", slack={"webhook_url": "https://fallback/z"}),
        ),
    )
    (tmp_path / "misc").mkdir()
    result = sinks_for(tmp_path / "misc", cfg)
    assert result is not None
    slack, _ = result
    assert slack.webhook_url == "https://fallback/z"
