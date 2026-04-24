from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Config:
    idle_threshold_seconds: float = 60.0
    gating: GatingMode = "idle_or_background"
    tool_input_max_chars: int = 400
    events: dict[EventKind, EventConfig] = field(default_factory=dict)
    slack: SlackConfig = field(default_factory=SlackConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)

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

    return Config(
        idle_threshold_seconds=idle,
        gating=gating,  # type: ignore[arg-type]
        tool_input_max_chars=tool_input_max_chars,
        events=events,
        slack=slack,
        discord=discord,
    )


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
"""
