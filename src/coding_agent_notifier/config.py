from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from . import keychain
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

Verbosity = Literal["terse", "normal", "minimal"]
VALID_VERBOSITIES: tuple[Verbosity, ...] = ("terse", "normal", "minimal")

# Per-workspace policy for freeform text inputs in interactive flows. "deny"
# suppresses the "Custom answer" (AskUserQuestion) and "Deny with reason"
# (PermissionRequest) modals so approvers can only choose fixed options. The
# daemon also rejects view_submissions for those callbacks defensively, in
# case a stale modal was opened before the policy flipped. Future modes
# (e.g. "audit", "redacted") slot in here without a config migration.
FreeformTextMode = Literal["allow", "deny"]
VALID_FREEFORM_TEXT_MODES: tuple[FreeformTextMode, ...] = ("allow", "deny")


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
    # Who can click Approve/Deny. `approver_user_ids` is a literal list of
    # Slack user IDs (U…); `approver_user_groups` is a list of Slack usergroup
    # (subteam) IDs (S…). A click is allowed if the user matches either list.
    # At least one list MUST be non-empty when actionable_approvals=true —
    # an empty allowlist would let anyone in the channel rubber-stamp a tool
    # call, which is a hard-fail of the security posture in a public or
    # shared channel. For a private DM with the bot, set your own user ID.
    approver_user_ids: tuple[str, ...] = ()
    approver_user_groups: tuple[str, ...] = ()
    approval_timeout_seconds: float = 300.0
    # Whether interactive flows may include freeform text inputs. Default
    # "allow" preserves the Custom-answer (AskUserQuestion) and Deny-with-
    # reason (PermissionRequest) modals. Set to "deny" to lock the workspace
    # to fixed-option clicks only — both trigger buttons are suppressed and
    # any view_submission for those modals is dropped by the daemon (with an
    # ephemeral hint posted back to the channel).
    freeform_text: FreeformTextMode = "allow"


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
    # Optional hard cap (chars) on the agent's message body in Slack/Discord
    # output. 0 = full text — only platform limits (Slack section: 3000,
    # Discord embed description: 4096) apply, and we split across multiple
    # blocks/embeds so the Slack client's auto-collapse handles long bodies.
    message_max_chars: int = 0
    # Head + tail chars to keep when a long message is collapsed behind a
    # Slack "Show more" button. The button only appears when the full body
    # exceeds `head + tail + 5` chars (matching `transcript.head_tail_snippet`'s
    # "no-op if short" threshold). Set either to 0 to disable that side; set
    # both to 0 to disable the toggle entirely (always post the full body).
    # Only applies to Slack with a bot_token — webhooks can't chat.update.
    message_preview_head_chars: int = 250
    message_preview_tail_chars: int = 250


@dataclass(frozen=True)
class SummaryConfig:
    enabled: bool = True
    # Soft cap on the transcript snippet that becomes `event.message` for
    # turn_complete / idle_prompt events. Sized larger than the default
    # `display.message_preview_*` budgets so the Show more / Show less
    # toggle has headroom to elide and expand. Set both to small values to
    # restore aggressive pre-truncation; set both to very large values to
    # effectively disable summary truncation (toggle then handles all UX).
    head_chars: int = 2000
    tail_chars: int = 2000


_SLACK_FIELD_KEYS = frozenset({
    "enabled",
    "webhook_url",
    "bot_token",
    "app_token",
    "channel",
    "interactive",
    "actionable_approvals",
    "approver_user_ids",
    "approver_user_groups",
    "approval_timeout_seconds",
    "freeform_text",
})
# Keys accepted in a [[routes]].slack block. `workspace` is extra: it names
# which `[slack.workspaces.<name>]` to use as the base, and is NOT a
# SlackConfig field — `_override_slack` consumes it before applying the
# remaining field overrides.
_VALID_SLACK_OVERRIDE_KEYS = _SLACK_FIELD_KEYS | {"workspace"}
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
    # Back-compat handle for the "default" workspace (or an empty config if
    # no default exists). All non-route code paths still read `config.slack`;
    # code that needs multi-workspace awareness walks `config.slack_workspaces`.
    slack: SlackConfig = field(default_factory=SlackConfig)
    slack_workspaces: dict[str, SlackConfig] = field(default_factory=dict)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    routes: tuple[Route, ...] = ()
    display: DisplayConfig = field(default_factory=DisplayConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)

    def event(self, kind: EventKind) -> EventConfig:
        return self.events.get(kind, EventConfig())

    def gating_for(self, kind: EventKind) -> GatingMode:
        return self.event(kind).gating or self.gating


def _resolve_secret(raw: dict[str, Any], key: str, *, context: str) -> str | None:
    """Resolve a secret from TOML via inline / env var / macOS Keychain.

    Precedence: inline `key` > `{key}_env` (env var lookup) > `{key}_keychain`
    (Keychain account label). A missing env var returns None — the caller's
    `enabled`/feature checks turn that into a clearer "feature X requires
    <key>" error. A configured Keychain lookup that fails is a different
    story: the user explicitly asked for a Keychain read, so both "account
    not in Keychain" and real subprocess failures raise ConfigError rather
    than silently returning None (see `keychain.py` docstring for why).

    `context` is a path string like "sinks.slack" or "slack.workspaces.home"
    that gets embedded in error messages to help the user locate the problem.
    """
    direct = raw.get(key)
    if isinstance(direct, str) and direct:
        return direct
    env_key = raw.get(f"{key}_env")
    if isinstance(env_key, str) and env_key:
        val = os.environ.get(env_key)
        if val:
            return val
    kc_account = raw.get(f"{key}_keychain")
    if isinstance(kc_account, str) and kc_account:
        try:
            val = keychain.read(kc_account)
        except keychain.KeychainError as e:
            raise ConfigError(
                f"{context}.{key}_keychain = {kc_account!r}: Keychain read failed: {e}"
            ) from e
        if val is None:
            raise ConfigError(
                f"{context}.{key}_keychain = {kc_account!r}: no entry in macOS Keychain. "
                f"Store one with `agent-notify slack add`, or fall back to "
                f"{key}_env / inline {key}."
            )
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
    from . import paths
    return paths.config_file()


def load_config(path: Path | None = None, *, stderr=None) -> Config:
    path = path or default_config_path()
    if not path.exists():
        # Even without a config.toml, a user might have secrets.toml sitting
        # alongside — but there's nothing for those secrets to fill in, so
        # treat it as an empty config rather than reading the secrets file.
        return Config()
    raw_text = path.read_text()
    _warn_on_loose_permissions(path, raw_text, stderr=stderr)
    raw = tomllib.loads(raw_text)
    secrets_path = path.with_name("secrets.toml")
    secrets_raw = _load_secrets_file(secrets_path)
    if secrets_raw:
        raw = _merge_secrets(raw, secrets_raw)
    return parse_config(raw)


def _load_secrets_file(path: Path) -> dict[str, Any]:
    """Read `secrets.toml` if present. Refuses to load on loose permissions.

    Unlike `config.toml` (which warns), `secrets.toml` exists solely to hold
    credentials — any group/world readability is treated as a real bug and
    hard-fails with a message pointing at `chmod 600`. Missing file is fine
    (returns {}).
    """
    if not path.exists():
        return {}
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as e:
        raise ConfigError(f"could not stat {path}: {e}") from e
    if mode & 0o077:
        raise ConfigError(
            f"{path} is mode {oct(mode)} — secrets.toml must be owner-only "
            f"(0600 or stricter). Run `chmod 600 {path}` and try again."
        )
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ConfigError(f"could not parse {path}: {e}") from e


def _merge_secrets(base: dict[str, Any], secrets: dict[str, Any]) -> dict[str, Any]:
    """Fill-in-missing deep merge of `secrets` into `base`.

    `base` (config.toml) wins at every leaf — secrets.toml is a fallback
    that supplies values config.toml left unset. Tables recurse; scalars in
    base are preserved. This rule prevents a loose secrets.toml from
    silently overriding intentional config.toml values.
    """
    merged = dict(base)
    for key, secret_val in secrets.items():
        if key not in merged:
            merged[key] = secret_val
        elif isinstance(merged[key], dict) and isinstance(secret_val, dict):
            merged[key] = _merge_secrets(merged[key], secret_val)
        # else: base has a non-dict value — keep it, ignore the secret
    return merged


def _warn_on_loose_permissions(path: Path, raw_text: str, *, stderr=None) -> None:
    """Warn loudly if the config is group/world-readable AND carries inline
    secrets. Doesn't parse TOML fully — a cheap substring scan is enough to
    decide whether the warning is warranted (false positive is fine; we'd
    rather over-warn than miss a real leak)."""
    import sys as _sys
    stderr = stderr if stderr is not None else _sys.stderr
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077 == 0:
        return  # owner-only; nothing to warn about
    secrets_present = any(
        _looks_like_inline_secret(raw_text, key)
        for key in ("webhook_url", "bot_token", "app_token")
    )
    if not secrets_present:
        return
    print(
        f"agent-notify: {path} is mode {oct(mode)} — group/world can read "
        f"it AND it contains inline secrets. Run `chmod 600 {path}` "
        f"(or move secrets to env vars and reference them via "
        f"`<key>_env = \"VAR_NAME\"`).",
        file=stderr,
    )


def _looks_like_inline_secret(text: str, key: str) -> bool:
    """True if `text` assigns `key = "..."` with a non-empty value.

    Cheap — doesn't handle every TOML quirk. `_env`-suffix keys are
    deliberately excluded (those reference env vars, not literal tokens).
    """
    import re as _re
    pattern = _re.compile(
        rf'^\s*{_re.escape(key)}\s*=\s*["\']([^"\']+)["\']',
        _re.MULTILINE,
    )
    return bool(pattern.search(text))


def _parse_slack_workspace(name: str, raw: dict[str, Any], *, context: str) -> SlackConfig:
    """Parse a single `[slack.workspaces.<name>]` entry. Validates feature
    prerequisites so an enabled-but-unconfigured workspace fails at parse
    time, not at first dispatch."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{context} must be a table")
    freeform_text = raw.get("freeform_text", "allow")
    if freeform_text not in VALID_FREEFORM_TEXT_MODES:
        raise ConfigError(
            f"{context}.freeform_text = {freeform_text!r} is invalid "
            f"(expected one of {list(VALID_FREEFORM_TEXT_MODES)})"
        )
    cfg = SlackConfig(
        enabled=bool(raw.get("enabled", False)),
        webhook_url=_resolve_secret(raw, "webhook_url", context=context),
        bot_token=_resolve_secret(raw, "bot_token", context=context),
        app_token=_resolve_secret(raw, "app_token", context=context),
        channel=raw.get("channel"),
        interactive=bool(raw.get("interactive", False)),
        actionable_approvals=bool(raw.get("actionable_approvals", False)),
        approver_user_ids=_parse_string_tuple(
            raw.get("approver_user_ids", []),
            field=f"{context}.approver_user_ids",
        ),
        approver_user_groups=_parse_string_tuple(
            raw.get("approver_user_groups", []),
            field=f"{context}.approver_user_groups",
        ),
        approval_timeout_seconds=float(raw.get("approval_timeout_seconds", 300.0)),
        freeform_text=freeform_text,  # type: ignore[arg-type]
    )
    if cfg.enabled and not (cfg.webhook_url or cfg.bot_token):
        raise ConfigError(f"{context} is enabled but has no webhook_url or bot_token")
    if cfg.interactive and not cfg.bot_token:
        raise ConfigError(f"{context}.interactive=true requires bot_token")
    if cfg.actionable_approvals:
        if not cfg.bot_token:
            raise ConfigError(f"{context}.actionable_approvals=true requires bot_token")
        if not cfg.app_token:
            raise ConfigError(
                f"{context}.actionable_approvals=true requires app_token "
                "(xapp-* for Socket Mode — get one from api.slack.com under Basic Information)"
            )
        # Secure-by-default approver gating. Two acceptable shapes:
        #
        #   1. An explicit allowlist (approver_user_ids or approver_user_groups).
        #      Required for any non-DM channel — anything shared must be gated.
        #
        #   2. Empty allowlist + `channel = "@me"` (DM with the bot). Only the
        #      installing user can see a DM with the bot, so the click origin
        #      is implicit. The daemon double-checks this at click time by
        #      asserting the incoming channel_id starts with `D` (Slack's DM
        #      prefix); if someone somehow routes the message into a non-DM,
        #      the runtime check fails closed rather than trusting the config.
        #
        # Anything else is a footgun and fails at parse time.
        channel = cfg.channel or "@me"
        if not cfg.approver_user_ids and not cfg.approver_user_groups:
            if channel != "@me":
                raise ConfigError(
                    f"{context}.actionable_approvals=true with channel="
                    f"{channel!r} requires a non-empty approver_user_ids or "
                    "approver_user_groups — otherwise anyone in the channel "
                    "can click Approve. An empty allowlist is only allowed "
                    "when channel is '@me' (DM with the bot)."
                )
    if cfg.approval_timeout_seconds < 0:
        raise ConfigError(
            f"{context}.approval_timeout_seconds must be >= 0 "
            f"(0 = wait forever — leave the approval open until resolved from "
            f"Slack on any device)"
        )
    return cfg


def _parse_slack_workspaces(raw: dict[str, Any]) -> dict[str, SlackConfig]:
    """Parse Slack workspaces from `[slack.workspaces.<name>]` blocks.

    The pre-v0.1 alias `[sinks.slack]` is no longer accepted — if it's
    present, raise a ConfigError that points at the new shape.
    """
    sinks = raw.get("sinks", {}) or {}
    if not isinstance(sinks, dict):
        raise ConfigError("sinks must be a table")
    legacy_raw = sinks.get("slack")
    if legacy_raw:
        raise ConfigError(
            "[sinks.slack] is no longer supported. Rename it to "
            "[slack.workspaces.default] (top-level `slack.workspaces.<name>` "
            "block). The wizard `agent-notify slack add` writes the new shape "
            "for you. See README §Slack."
        )

    slack_top = raw.get("slack", {}) or {}
    if not isinstance(slack_top, dict):
        raise ConfigError("slack must be a table")
    workspaces_raw = slack_top.get("workspaces", {}) or {}
    if not isinstance(workspaces_raw, dict):
        raise ConfigError("slack.workspaces must be a table")

    workspaces: dict[str, SlackConfig] = {}
    for name, ws_raw in workspaces_raw.items():
        if not isinstance(name, str) or not name:
            raise ConfigError("slack.workspaces keys must be non-empty strings")
        if not isinstance(ws_raw, dict):
            raise ConfigError(f"slack.workspaces.{name} must be a table")
        workspaces[name] = _parse_slack_workspace(
            name, ws_raw, context=f"slack.workspaces.{name}"
        )
    return workspaces


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

    slack_workspaces = _parse_slack_workspaces(raw)
    # Back-compat handle: `config.slack` == the "default" workspace, or empty
    # if none was defined. Old single-workspace code paths keep working.
    slack = slack_workspaces.get("default", SlackConfig())

    discord_sinks = raw.get("sinks", {}) or {}
    discord_raw = discord_sinks.get("discord", {}) or {}
    if not isinstance(discord_raw, dict):
        raise ConfigError("sinks.discord must be a table")
    discord = DiscordConfig(
        enabled=bool(discord_raw.get("enabled", False)),
        webhook_url=_resolve_secret(discord_raw, "webhook_url", context="sinks.discord"),
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
        ws_ref = slack_override.get("workspace")
        if ws_ref is not None:
            if not isinstance(ws_ref, str) or not ws_ref:
                raise ConfigError(
                    f"routes[{i}].slack.workspace must be a non-empty string"
                )
            if ws_ref not in slack_workspaces:
                known = sorted(slack_workspaces) or ["(none defined)"]
                raise ConfigError(
                    f"routes[{i}].slack.workspace = {ws_ref!r} refs an undefined "
                    f"workspace. Known workspaces: {known}"
                )
        # Coerce tuple-typed overrides so `replace()` downstream preserves
        # the dataclass's declared types. Without this, a route that sets
        # `approver_user_ids = [...]` leaves a list where SlackConfig expects
        # a tuple, which breaks both comparisons and hashability.
        slack_override_clean = dict(slack_override)
        for tuple_field in ("approver_user_ids", "approver_user_groups"):
            if tuple_field in slack_override_clean:
                slack_override_clean[tuple_field] = _parse_string_tuple(
                    slack_override_clean[tuple_field],
                    field=f"routes[{i}].slack.{tuple_field}",
                )
        if "freeform_text" in slack_override_clean:
            value = slack_override_clean["freeform_text"]
            if value not in VALID_FREEFORM_TEXT_MODES:
                raise ConfigError(
                    f"routes[{i}].slack.freeform_text = {value!r} is invalid "
                    f"(expected one of {list(VALID_FREEFORM_TEXT_MODES)})"
                )
        routes.append(Route(cwd=cwd, slack=slack_override_clean, discord=dict(discord_override)))

    display_raw = raw.get("display", {}) or {}
    if not isinstance(display_raw, dict):
        raise ConfigError("display must be a table")
    verbosity = display_raw.get("verbosity", "terse")
    if verbosity not in VALID_VERBOSITIES:
        raise ConfigError(f"invalid display.verbosity: {verbosity!r}")
    coalesce_window = float(display_raw.get("coalesce_window_seconds", 2.5))
    if coalesce_window < 0:
        raise ConfigError(f"display.coalesce_window_seconds must be >= 0, got {coalesce_window}")
    message_max_chars = int(display_raw.get("message_max_chars", 0))
    if message_max_chars < 0:
        raise ConfigError(
            f"display.message_max_chars must be >= 0 (0 = full), got {message_max_chars}"
        )
    preview_head = int(display_raw.get("message_preview_head_chars", 250))
    if preview_head < 0:
        raise ConfigError(
            f"display.message_preview_head_chars must be >= 0, got {preview_head}"
        )
    preview_tail = int(display_raw.get("message_preview_tail_chars", 250))
    if preview_tail < 0:
        raise ConfigError(
            f"display.message_preview_tail_chars must be >= 0, got {preview_tail}"
        )
    display = DisplayConfig(
        verbosity=verbosity,  # type: ignore[arg-type]
        coalesce_window_seconds=coalesce_window,
        message_max_chars=message_max_chars,
        message_preview_head_chars=preview_head,
        message_preview_tail_chars=preview_tail,
    )

    summary_raw = raw.get("summary", {}) or {}
    if not isinstance(summary_raw, dict):
        raise ConfigError("summary must be a table")
    head_chars = int(summary_raw.get("head_chars", 2000))
    tail_chars = int(summary_raw.get("tail_chars", 2000))
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
        slack_workspaces=slack_workspaces,
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
      that route's overrides. `route.slack.workspace = "name"` swaps in that
      named workspace as the slack base before applying per-route fields.
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
    slack = _override_slack(config.slack, route.slack, workspaces=config.slack_workspaces)
    discord = _override_discord(config.discord, route.discord)
    return slack, discord


def workspace_for(cwd: Path, config: Config) -> str:
    """Return the workspace name selected for this cwd.

    Matches `sinks_for`'s resolution: if routing selects a named workspace,
    that name; otherwise "default". Callers use this to stamp the approval
    record so the hook's timeout-cleanup path can pick the right bot_token.
    """
    if not config.routes:
        return "default"
    route = match_route(cwd, config)
    if route is None:
        return "default"
    ws_ref = route.slack.get("workspace")
    if isinstance(ws_ref, str) and ws_ref:
        return ws_ref
    return "default"


def _override_slack(
    base: SlackConfig,
    override: dict[str, Any],
    *,
    workspaces: dict[str, SlackConfig],
) -> SlackConfig:
    if not override:
        return base
    # `workspace` selects the base workspace; the remaining keys patch its fields.
    # Validated at parse time, so an unknown name here is a programmer error.
    ws_ref = override.get("workspace")
    if isinstance(ws_ref, str) and ws_ref in workspaces:
        base = workspaces[ws_ref]
    field_overrides = {k: override[k] for k in override if k in _SLACK_FIELD_KEYS}
    if not field_overrides:
        return base
    return replace(base, **field_overrides)


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
verbosity = "terse"           # terse | normal | minimal
                               # - terse   : compact layout, 1-line iOS preview, full body
                               # - normal  : explicit Project/Session/Tool/App fields
                               # - minimal : ONLY "Agent needs attention" — no tool_name,
                               #             tool_input, message body, transcript snippet,
                               #             cwd, session id, or source app ever hits
                               #             Slack/Discord. Use this in compliance-sensitive
                               #             or shared-channel environments.
coalesce_window_seconds = 2.5  # hold turn_complete this long so a follow-up idle_prompt can cancel it. 0 disables.
message_max_chars = 0          # 0 = full text. Slack/Discord clients auto-collapse long messages;
                               # we split across blocks/embeds when over platform limits (Slack 3000,
                               # Discord 4096). Set a positive int to hard-cap the message body.
message_preview_head_chars = 250  # Slack only (bot_token, not webhook). When the body is longer than
message_preview_tail_chars = 250  # head+tail+5, post a head…tail preview + a "Show more" button that
                                  # rewrites the message in place to the full body. Set both to 0 to
                                  # disable; ignored when verbosity="minimal" or message_max_chars>0.

[summary]
enabled = true                 # include a head/tail snippet of the agent's last message on turn_complete
head_chars = 2000              # Soft cap on the snippet that becomes event.message. Sized larger than
tail_chars = 2000              # display.message_preview_*chars so the Slack Show more / Show less toggle
                                # has room to elide and expand. Set both small (e.g. 250/250) to restore
                                # aggressive pre-truncation; set both very large to defer all UX
                                # truncation to the toggle.

# --- Slack workspaces ----------------------------------------------------
#
# Define one or more Slack workspaces under [slack.workspaces.<name>]. The
# wizard (`agent-notify slack add`) writes these for you and stores bot /
# app tokens in macOS Keychain.
#
# Secret resolution for each token tries, in order:
#   1. Inline value                             (bot_token = "xoxb-…")
#   2. Environment variable                     (bot_token_env = "VAR_NAME")
#   3. macOS Keychain                           (bot_token_keychain = "<workspace>:bot_token")
#   4. secrets.toml (sibling file)              — see §Secrets file below
#
# [slack.workspaces.default]
# enabled = true
# bot_token_keychain = "default:bot_token"      # xoxb-*
# app_token_keychain = "default:app_token"      # xapp-* — only for actionable_approvals
# channel = "@me"                               # DM with the bot (secure default)
# interactive = true                            # approve/deny buttons on pings
# actionable_approvals = true                   # block PermissionRequest, inject decision
# approver_user_ids = ["U0YOURID"]              # wizard fills this in automatically
# approver_user_groups = ["S01OPSTEAM"]         # optional: allow a Slack usergroup
# freeform_text = "allow"                       # "allow" (default) | "deny" — set to "deny" to
#                                                 # suppress the Custom-answer (AskUserQuestion)
#                                                 # and Deny-with-reason modals so approvers can
#                                                 # only click fixed options. The daemon also
#                                                 # rejects view_submissions for those modals
#                                                 # defensively when "deny".
# approval_timeout_seconds = 300                # hook fails closed (deny) after timeout.
#                                                 # Set to 0 to wait forever — the approval
#                                                 # stays pending until resolved from Slack on
#                                                 # any device (no auto-deny). Note: Claude
#                                                 # Code's own hook timeout (in settings.json)
#                                                 # also caps how long the subprocess can wait.
#
# Empty allowlist (no approver_user_ids, no approver_user_groups) is only
# accepted when `channel = "@me"` — the daemon double-checks at click time
# that the Slack channel ID starts with `D` (DM prefix), so stray posts to
# shared channels can never rubber-stamp tool calls. Any shared/public
# channel requires an explicit allowlist.
#
# For webhook-only pings (no buttons, no approvals), the same block works
# with just a webhook URL:
# [slack.workspaces.default]
# enabled = true
# webhook_url = "https://hooks.slack.com/services/…"

[sinks.discord]
enabled = false
# webhook_url = "https://discord.com/api/webhooks/…"

# Per-repo routing. First matching route wins. Paths expand `~` and support
# `fnmatch` wildcards (`*`, `?`, `[abc]`). Each route can:
#   - select a different workspace via `slack.workspace = "work"`
#   - override any individual field (channel, approver_user_ids, …)
#
# *** STRICT MODE ***
# As soon as any [[routes]] entry exists, unmatched cwds send NOTHING — we
# never fall back to the default workspace. Add a catch-all (cwd = "*") at
# the end if you want a fallback destination.
#
# [[routes]]
# cwd = "~/work/acme-*"
# slack.workspace = "work"         # references [slack.workspaces.work]
# slack.channel = "#agents-acme"
#
# [[routes]]
# cwd = "~/personal/*"
# slack.workspace = "default"
# slack.channel = "@me"
#
# [[routes]]
# cwd = "*"                        # explicit catch-all (opt-in to fallback)
# slack.workspace = "default"

# --- Secrets file (optional) ---------------------------------------------
#
# If you want to keep bot tokens out of this file (e.g. to commit config.toml
# to a dotfiles repo), drop them into a sibling `secrets.toml` with the same
# shape. Values there fill in missing keys here; if a key is set in BOTH
# files, config.toml wins. secrets.toml permissions are ENFORCED at 0600 —
# loose perms abort the load rather than warn.
#
#   ~/.agent-notify/config.toml   (0600)   — structure, routing, non-secrets
#   ~/.agent-notify/secrets.toml  (0600)   — just the token values
#
# Example ~/.agent-notify/secrets.toml:
#
#   [slack.workspaces.default]
#   bot_token = "xoxb-…"
#   app_token = "xapp-…"
"""
