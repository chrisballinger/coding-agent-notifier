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

Verbosity = Literal["terse", "normal"]
VALID_VERBOSITIES: tuple[Verbosity, ...] = ("terse", "normal")


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
    app_token: str | None = None
    channel: str | None = None
    interactive: bool = False
    actionable_approvals: bool = False
    approver_user_ids: tuple[str, ...] = ()
    approval_timeout_seconds: float = 600.0


@dataclass(frozen=True)
class DiscordConfig:
    enabled: bool = False
    webhook_url: str | None = None


@dataclass(frozen=True)
class DisplayConfig:
    verbosity: Verbosity = "terse"
    # Seconds to hold a `turn_complete` ping before dispatching so that a
    # follow-up `idle_prompt` can cancel it. 0 disables coalescing.
    coalesce_window_seconds: float = 2.5


@dataclass(frozen=True)
class SummaryConfig:
    enabled: bool = True
    head_chars: int = 250
    tail_chars: int = 250


_VALID_SLACK_OVERRIDE_KEYS = frozenset({
    "enabled",
    "webhook_url",
    "bot_token",
    "app_token",
    "channel",
    "interactive",
    "actionable_approvals",
    "approver_user_ids",
    "approval_timeout_seconds",
})
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
    display: DisplayConfig = field(default_factory=DisplayConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)

    def event(self, kind: EventKind) -> EventConfig:
        return self.events.get(kind, EventConfig())

    def gating_for(self, kind: EventKind) -> GatingMode:
        return self.event(kind).gating or self.gating


def _resolve_env(raw: dict[str, Any], key: str) -> str | None:
    """Resolve a secret from TOML.

    Prefer an inline value at `key`; fall back to `os.environ[raw[f"{key}_env"]]`.
    A missing env var returns None rather than raising — the `enabled`/feature
    checks catch the real misconfiguration with a clearer message.
    """
    direct = raw.get(key)
    if isinstance(direct, str) and direct:
        return direct
    env_key = raw.get(f"{key}_env")
    if isinstance(env_key, str) and env_key:
        val = os.environ.get(env_key)
        if val:
            return val
    return None


def _parse_string_tuple(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{field} must be an array of strings")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(f"{field}[{i}] must be a string, got {type(item).__name__}")
        out.append(item)
    return tuple(out)


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
        webhook_url=_resolve_env(slack_raw, "webhook_url"),
        bot_token=_resolve_env(slack_raw, "bot_token"),
        app_token=_resolve_env(slack_raw, "app_token"),
        channel=slack_raw.get("channel"),
        interactive=bool(slack_raw.get("interactive", False)),
        actionable_approvals=bool(slack_raw.get("actionable_approvals", False)),
        approver_user_ids=_parse_string_tuple(slack_raw.get("approver_user_ids", []),
                                              field="sinks.slack.approver_user_ids"),
        approval_timeout_seconds=float(slack_raw.get("approval_timeout_seconds", 600.0)),
    )
    if slack.enabled and not (slack.webhook_url or slack.bot_token):
        raise ConfigError("sinks.slack is enabled but has no webhook_url or bot_token")
    if slack.interactive and not slack.bot_token:
        raise ConfigError("sinks.slack.interactive=true requires bot_token")
    if slack.actionable_approvals:
        if not slack.bot_token:
            raise ConfigError("sinks.slack.actionable_approvals=true requires bot_token")
        if not slack.app_token:
            raise ConfigError(
                "sinks.slack.actionable_approvals=true requires app_token "
                "(xapp-* for Socket Mode — get one from api.slack.com under Basic Information)"
            )
    if slack.approval_timeout_seconds <= 0:
        raise ConfigError("sinks.slack.approval_timeout_seconds must be > 0")

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

    display_raw = raw.get("display", {}) or {}
    if not isinstance(display_raw, dict):
        raise ConfigError("display must be a table")
    verbosity = display_raw.get("verbosity", "terse")
    if verbosity not in VALID_VERBOSITIES:
        raise ConfigError(f"invalid display.verbosity: {verbosity!r}")
    coalesce_window = float(display_raw.get("coalesce_window_seconds", 2.5))
    if coalesce_window < 0:
        raise ConfigError(f"display.coalesce_window_seconds must be >= 0, got {coalesce_window}")
    display = DisplayConfig(
        verbosity=verbosity,  # type: ignore[arg-type]
        coalesce_window_seconds=coalesce_window,
    )

    summary_raw = raw.get("summary", {}) or {}
    if not isinstance(summary_raw, dict):
        raise ConfigError("summary must be a table")
    head_chars = int(summary_raw.get("head_chars", 250))
    tail_chars = int(summary_raw.get("tail_chars", 250))
    if head_chars < 0 or tail_chars < 0:
        raise ConfigError("summary.head_chars and summary.tail_chars must be >= 0")
    summary = SummaryConfig(
        enabled=bool(summary_raw.get("enabled", True)),
        head_chars=head_chars,
        tail_chars=tail_chars,
    )

    return Config(
        idle_threshold_seconds=idle,
        gating=gating,  # type: ignore[arg-type]
        tool_input_max_chars=tool_input_max_chars,
        events=events,
        slack=slack,
        discord=discord,
        routes=tuple(routes),
        display=display,
        summary=summary,
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

[display]
verbosity = "terse"           # terse | normal. terse drops the 4-field block in favor of a compact footer.
coalesce_window_seconds = 2.5  # hold turn_complete this long so a follow-up idle_prompt can cancel it. 0 disables.

[summary]
enabled = true                 # include a head/tail snippet of the agent's last message on turn_complete
head_chars = 250
tail_chars = 250

[sinks.slack]
# Set enabled = true after filling in a webhook_url or bot_token below.
enabled = false
# Pick ONE of:
# webhook_url = "https://hooks.slack.com/services/…"
# bot_token = "xoxb-…"                # or bot_token_env = "SLACK_BOT_TOKEN"
# channel = "@me"                     # resolved via auth.test when @me

# Interactive + actionable approvals (requires bot_token and Slack App with Socket Mode).
# See docs/plans/slack-bot-interactive.md for the full setup flow.
# interactive = true                  # approve/deny buttons on permission messages
# actionable_approvals = true         # wire the blocking PreToolUse hook — approvals
                                       # actually resolve the pending tool call, not
                                       # just notify
# app_token_env = "SLACK_APP_TOKEN"    # xapp-* app-level token (Socket Mode)
# approver_user_ids = ["U0123ABC"]    # Slack user IDs allowed to click buttons
# approval_timeout_seconds = 600      # hook blocks this long before failing closed (deny)

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
