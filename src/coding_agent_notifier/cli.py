from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from . import __version__, dedup, install, macos
from .config import CONFIG_TEMPLATE, Config, default_config_path, load_config
from .event import Event
from .gating import SystemState, should_send

PERMISSION_DEDUP_TTL = 5.0
from .sinks.base import Sink, SinkError
from .sinks.discord import DiscordSink
from .sinks.slack import SlackSink
from .sources import claude_code as src_claude
from .sources import codex as src_codex


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-notify",
        description="Pings you (Slack/Discord) when a coding agent needs attention.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", type=Path, help="Override config file path.")
    sub = p.add_subparsers(dest="cmd", required=True)

    hook = sub.add_parser("hook", help="Process an agent hook payload on stdin.")
    hook.add_argument("--source", required=True, choices=["claude-code", "codex"])
    hook.add_argument(
        "--force",
        action="store_true",
        help="Bypass gating and always dispatch (for debugging).",
    )

    cfg = sub.add_parser("config", help="Config management.")
    cfg_sub = cfg.add_subparsers(dest="cfg_cmd", required=True)
    cfg_sub.add_parser("path", help="Print the config file path.")
    cfg_sub.add_parser("init", help="Write a commented config template.")

    inst = sub.add_parser("install", help="Install hooks into an agent's config.")
    inst.add_argument("target", choices=["claude-code", "codex"])

    test = sub.add_parser("test", help="Send a synthetic test event through sinks.")
    test.add_argument("--kind", default="permission",
                      choices=["permission", "idle_prompt", "turn_complete", "elicitation"])
    test.add_argument("--force", action="store_true", help="Bypass gating.")

    sub.add_parser("doctor", help="Check config, connectivity, and install state.")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "hook":
            return cmd_hook(args)
        if args.cmd == "config":
            return cmd_config(args)
        if args.cmd == "install":
            return cmd_install(args)
        if args.cmd == "test":
            return cmd_test(args)
        if args.cmd == "doctor":
            return cmd_doctor(args)
    except Exception:  # noqa: BLE001 - never block the agent
        traceback.print_exc(file=sys.stderr)
        return 0
    return 0


# ---- commands ----


def cmd_hook(args: argparse.Namespace) -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"agent-notify: malformed hook JSON: {e}", file=sys.stderr)
        return 0

    source_app = macos.term_program_to_app(os.environ.get("TERM_PROGRAM"))
    parse = src_claude.parse if args.source == "claude-code" else src_codex.parse
    event = parse(payload, source_app=source_app)
    if event is None:
        return 0

    if not args.force and _is_duplicate(event):
        return 0

    config = load_config(args.config)
    if not args.force and not should_send(event, config, _snapshot_state()):
        return 0
    _dispatch(event, config)
    return 0


def _is_duplicate(event: Event) -> bool:
    # Claude Code fires both `PermissionRequest` and `Notification:permission_prompt`
    # for the same approval gate. Collapse them within a short TTL so the user
    # only gets one Slack ping per approval. We key on (agent, session) — not
    # tool_name — because the Notification payload doesn't carry the tool.
    if event.kind != "permission":
        return False
    key = dedup.dedup_key(event.agent, event.kind, event.session_id, None)
    return dedup.recently_seen(key, ttl=PERMISSION_DEDUP_TTL)


def cmd_config(args: argparse.Namespace) -> int:
    path = args.config or default_config_path()
    if args.cfg_cmd == "path":
        print(path)
        return 0
    if args.cfg_cmd == "init":
        if path.exists():
            print(f"agent-notify: {path} already exists; not overwriting", file=sys.stderr)
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFIG_TEMPLATE)
        print(f"Wrote {path}")
        return 0
    return 1


def cmd_install(args: argparse.Namespace) -> int:
    if args.target == "claude-code":
        added = install.install_claude_code()
        if added:
            print(f"Claude Code: added hooks for {', '.join(added)}")
        else:
            print("Claude Code: hooks already installed; nothing to do.")
        return 0
    summary = install.install_codex()
    if summary["hooks_added"]:
        print(f"Codex: added hooks for {', '.join(summary['hooks_added'])}")
    if summary["config_updated"]:
        print("Codex: updated ~/.codex/config.toml (notify + features.codex_hooks)")
    if not summary["hooks_added"] and not summary["config_updated"]:
        print("Codex: already installed; nothing to do.")
    if "previous_notify" in summary:
        print(
            f"Codex: previous notify = {summary['previous_notify']} was replaced.",
            file=sys.stderr,
        )
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    event = Event(
        agent="claude-code",
        kind=args.kind,
        message="Synthetic test event from agent-notify",
        cwd=Path.cwd(),
        session_id="test1234",
        tool_name="Bash" if args.kind == "permission" else None,
        tool_input_preview="echo 'hello from agent-notify'" if args.kind == "permission" else None,
        source_app=macos.term_program_to_app(os.environ.get("TERM_PROGRAM")),
    )
    config = load_config(args.config)
    if not args.force and not should_send(event, config, _snapshot_state()):
        print(
            "Gating suppressed the test event (use --force to bypass).",
            file=sys.stderr,
        )
        return 0
    _dispatch(event, config)
    print("Test event dispatched.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    path = args.config or default_config_path()
    print(f"Config: {path}  {'(exists)' if path.exists() else '(missing)'}")
    try:
        config = load_config(args.config)
    except Exception as e:  # noqa: BLE001
        print(f"  ! failed to load: {e}")
        return 1
    print(f"  gating: {config.gating}  idle_threshold: {config.idle_threshold_seconds}s")
    print(f"  slack:   enabled={config.slack.enabled} webhook={bool(config.slack.webhook_url)} bot={bool(config.slack.bot_token)}")
    print(f"  discord: enabled={config.discord.enabled} webhook={bool(config.discord.webhook_url)}")

    state = _snapshot_state()
    print(f"System: idle={state.idle_seconds}s frontmost={state.frontmost_app!r}")

    claude_settings = Path.home() / ".claude" / "settings.json"
    print(f"Claude settings: {claude_settings}  {'(exists)' if claude_settings.exists() else '(missing)'}")
    codex_config = Path.home() / ".codex" / "config.toml"
    print(f"Codex config:    {codex_config}  {'(exists)' if codex_config.exists() else '(missing)'}")
    return 0


# ---- helpers ----


def _snapshot_state() -> SystemState:
    return SystemState(
        idle_seconds=macos.idle_seconds(),
        frontmost_app=macos.frontmost_app(),
    )


def _dispatch(event: Event, config: Config) -> None:
    sinks: list[Sink] = []
    if config.slack.enabled:
        sinks.append(SlackSink(config.slack))
    if config.discord.enabled:
        sinks.append(DiscordSink(config.discord))
    for sink in sinks:
        try:
            sink.send(event)
        except SinkError as e:
            print(f"agent-notify: {sink.name} sink failed: {e}", file=sys.stderr)
