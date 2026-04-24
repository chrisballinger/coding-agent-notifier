from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from .event import EventKind

GatingMode = Literal["idle_only", "background_only", "idle_or_background", "always"]
VALID_GATING_MODES: tuple[GatingMode, ...] = (
    "idle_only",
    "background_only",
    "idle_or_background",
    "always",
)
VALID_EVENT_KINDS: tuple[EventKind, ...] = (
    "permission",
    "idle_prompt",
    "turn_complete",
    "elicitation",
)


class ConfigError(ValueError):
    """Raised when the config file is missing fields, misshapen, or invalid."""


@dataclass(frozen=True)
class EventConfig:
    enabled: bool = True
    gating: GatingMode | None = None  # None → fall back to Config.gating


@dataclass(frozen=True)
class SlackConfig:
    enabled: bool = False
    webhook_url: str | None = None
    bot_token: str | None = None
    channel: str | None = None


@dataclass(frozen=True)
class DiscordConfig:
    enabled: bool = False
    webhook_url: str | None = None


_VALID_SLACK_OVERRIDE_KEYS = frozenset({"enabled", "webhook_url", "bot_token", "channel"})
_VALID_DISCORD_OVERRIDE_KEYS = frozenset({"enabled", "webhook_url"})


@dataclass(frozen=True)
class Route:
    """A `cwd` glob + partial sink overrides applied when a repo path matches."""

    cwd: str
    slack: dict[str, Any] = field(default_factory=dict)
    discord: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    idle_threshold_seconds: float = 60.0
    gating: GatingMode = "idle_or_background"
    tool_input_max_chars: int = 400
    events: dict[EventKind, EventConfig] = field(default_factory=dict)
    slack: SlackConfig = field(default_factory=SlackConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    routes: tuple[Route, ...] = ()

    def event(self, kind: EventKind) -> EventConfig:
        return self.events.get(kind, EventConfig())

    def gating_for(self, kind: EventKind) -> GatingMode:
        return self.event(kind).gating or self.gating


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "coding-agent-notifier" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text())
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> Config:
    idle = float(raw.get("idle_threshold_seconds", 60))
    gating = raw.get("gating", "idle_or_background")
    if gating not in VALID_GATING_MODES:
        raise ConfigError(f"invalid gating mode: {gating!r}")
    tool_input_max_chars = int(raw.get("tool_input_max_chars", 400))
    if tool_input_max_chars <= 0:
        raise ConfigError(f"tool_input_max_chars must be positive, got {tool_input_max_chars}")

    events_raw = raw.get("events", {}) or {}
    events: dict[EventKind, EventConfig] = {}
    for k, v in events_raw.items():
        if k not in VALID_EVENT_KINDS:
            raise ConfigError(f"unknown event kind: {k!r}")
        if not isinstance(v, dict):
            raise ConfigError(f"events.{k} must be a table")
        mode = v.get("gating")
        if mode is not None and mode not in VALID_GATING_MODES:
            raise ConfigError(f"invalid gating mode for events.{k}: {mode!r}")
        events[k] = EventConfig(  # type: ignore[index]
            enabled=bool(v.get("enabled", True)),
            gating=mode,
        )

    sinks = raw.get("sinks", {}) or {}
    slack_raw = sinks.get("slack", {}) or {}
    slack = SlackConfig(
        enabled=bool(slack_raw.get("enabled", False)),
        webhook_url=slack_raw.get("webhook_url"),
        bot_token=slack_raw.get("bot_token"),
        channel=slack_raw.get("channel"),
    )
    if slack.enabled and not (slack.webhook_url or slack.bot_token):
        raise ConfigError("sinks.slack is enabled but has no webhook_url or bot_token")

    discord_raw = sinks.get("discord", {}) or {}
    discord = DiscordConfig(
        enabled=bool(discord_raw.get("enabled", False)),
        webhook_url=discord_raw.get("webhook_url"),
    )

    routes_raw = raw.get("routes", []) or []
    if not isinstance(routes_raw, list):
        raise ConfigError("routes must be an array of tables")
    routes: list[Route] = []
    for i, entry in enumerate(routes_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"routes[{i}] must be a table")
        cwd = entry.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            raise ConfigError(f"routes[{i}].cwd must be a non-empty string")
        slack_override = entry.get("slack", {}) or {}
        discord_override = entry.get("discord", {}) or {}
        if not isinstance(slack_override, dict):
            raise ConfigError(f"routes[{i}].slack must be a table")
        if not isinstance(discord_override, dict):
            raise ConfigError(f"routes[{i}].discord must be a table")
        unknown = set(slack_override) - _VALID_SLACK_OVERRIDE_KEYS
        if unknown:
            raise ConfigError(f"routes[{i}].slack has unknown keys: {sorted(unknown)}")
        unknown = set(discord_override) - _VALID_DISCORD_OVERRIDE_KEYS
        if unknown:
            raise ConfigError(f"routes[{i}].discord has unknown keys: {sorted(unknown)}")
        routes.append(Route(cwd=cwd, slack=dict(slack_override), discord=dict(discord_override)))

    return Config(
        idle_threshold_seconds=idle,
        gating=gating,  # type: ignore[arg-type]
        tool_input_max_chars=tool_input_max_chars,
        events=events,
        slack=slack,
        discord=discord,
        routes=tuple(routes),
    )


def match_route(cwd: Path, config: Config) -> Route | None:
    """Return the first route whose `cwd` glob matches the given path.

    The glob supports `~` expansion and `fnmatch` wildcards. Matching runs
    against the absolute, symlink-resolved form of `cwd` so relative paths
    and `~/project` both land at the same canonical string.
    """
    try:
        target = str(cwd.expanduser().resolve())
    except (OSError, RuntimeError):
        target = str(cwd)
    for route in config.routes:
        pattern = os.path.expanduser(route.cwd)
        if fnmatch.fnmatch(target, pattern):
            return route
    return None


def sinks_for(cwd: Path, config: Config) -> tuple[SlackConfig, DiscordConfig] | None:
    """Resolve sink configs for this repo path, honoring strict routing.

    - No routes configured → fall back to the global `[sinks.*]` blocks.
    - Routes configured and one matches → return the base sinks merged with
      that route's overrides.
    - Routes configured and none match → return None. The caller treats this
      as "skip this dispatch" so an unrouted repo never accidentally pings a
      channel belonging to a different project. Users who want a default can
      add an explicit catch-all route (`cwd = "*"`).
    """
    if not config.routes:
        return config.slack, config.discord
    route = match_route(cwd, config)
    if route is None:
        return None
    slack = _override_slack(config.slack, route.slack)
    discord = _override_discord(config.discord, route.discord)
    return slack, discord


def _override_slack(base: SlackConfig, override: dict[str, Any]) -> SlackConfig:
    if not override:
        return base
    return replace(base, **{k: override[k] for k in override if k in _VALID_SLACK_OVERRIDE_KEYS})


def _override_discord(base: DiscordConfig, override: dict[str, Any]) -> DiscordConfig:
    if not override:
        return base
    return replace(base, **{k: override[k] for k in override if k in _VALID_DISCORD_OVERRIDE_KEYS})


CONFIG_TEMPLATE = """\
# coding-agent-notifier config
idle_threshold_seconds = 60
gating = "idle_or_background"  # idle_only | background_only | idle_or_background | always
tool_input_max_chars = 400     # truncation cap for tool input code blocks in notifications

[events.permission]
enabled = true
gating = "always"               # approvals are urgent — always ping

[events.idle_prompt]
enabled = true

[events.elicitation]
enabled = true

[events.turn_complete]
enabled = true

[sinks.slack]
# Set enabled = true after filling in a webhook_url or bot_token below.
enabled = false
# Pick ONE of:
# webhook_url = "https://hooks.slack.com/services/…"
# bot_token = "xoxb-…"
# channel = "@me"              # resolved via auth.test when @me

[sinks.discord]
enabled = false
# webhook_url = "https://discord.com/api/webhooks/…"

# Per-repo routing. First matching route wins. Paths expand `~` and support
# `fnmatch` wildcards (`*`, `?`, `[abc]`). Override just the fields you want
# to change; everything else inherits from the sink blocks above.
#
# *** STRICT MODE ***
# As soon as any [[routes]] entry exists, unmatched cwds send NOTHING — we
# never fall back to [sinks.slack] for safety. Add a catch-all route (cwd =
# "*") at the end if you want a default ping destination.
#
# [[routes]]
# cwd = "~/work/acme-*"
# slack.webhook_url = "https://hooks.slack.com/services/acme-work/…"
#
# [[routes]]
# cwd = "~/personal/*"
# slack.channel = "#me-only"       # overrides channel when using a bot_token
#
# [[routes]]
# cwd = "*"                        # explicit catch-all (opt-in to fallback)
# slack.webhook_url = "https://hooks.slack.com/services/default/…"
"""
