from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from dataclasses import replace
from pathlib import Path

from . import __version__, dedup, install, macos, pending, transcript
from .config import CONFIG_TEMPLATE, Config, default_config_path, load_config, match_route, sinks_for
from .event import Event
from .gating import SystemState, should_send

# Per-kind dedup windows. Permission gets a much longer window because Claude
# Code's `Notification:permission_prompt` can fire tens of seconds after the
# `PermissionRequest` hook for the same logical event (especially when the user
# is slow to respond). Both hooks fire per-session so collisions across distinct
# permissions within 60s are unusual enough to accept as a tradeoff.
DEDUP_TTLS: dict[str, float] = {
    "permission": 60.0,
    "turn_complete": 5.0,
}
from .sinks.base import Sink, SinkError
from .sinks.discord import DiscordSink
from .sinks.slack import SlackSink
from .sources import claude_code as src_claude
from .sources import codex as src_codex

# Indirection so tests can neutralize the sleep without touching `time`.
_sleep = time.sleep


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
    test.add_argument("--dangerous", action="store_true",
                      help="Use a dangerous command in the synthetic tool_input (for permission kind).")

    sub.add_parser("doctor", help="Check config, connectivity, and install state.")

    # Hidden: internal deferred-dispatch subcommand invoked by a detached child
    # to coalesce a queued turn_complete after a short window.
    defer = sub.add_parser("_defer-dispatch", add_help=False)
    defer.add_argument("agent")
    defer.add_argument("session_id", nargs="?", default="")

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
        if args.cmd == "_defer-dispatch":
            return cmd_defer_dispatch(args)
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

    # An idle_prompt supersedes a queued turn_complete for the same session —
    # discard the queued event so the deferred dispatcher's subsequent claim
    # returns None.
    if event.kind == "idle_prompt":
        pending.claim(event.agent, event.session_id)

    if (
        event.kind == "turn_complete"
        and config.display.coalesce_window_seconds > 0
        and not args.force
    ):
        if not should_send(event, config, _snapshot_state()):
            return 0
        pending.write(event)
        _log_event(
            f"hook queued turn_complete agent={event.agent} "
            f"sess={event.session_id} spawning={sys.executable}"
        )
        _spawn_defer_child(args.config, event.agent, event.session_id)
        return 0

    if not args.force and not should_send(event, config, _snapshot_state()):
        return 0
    event = _maybe_apply_snippet(event, config)
    _dispatch(event, config)
    return 0


def cmd_defer_dispatch(args: argparse.Namespace) -> int:
    # Retained for backward compatibility / manual invocation; the production
    # path uses double-fork `_run_defer_inline` and never re-execs through argv.
    _run_defer_inline(args.config, args.agent, args.session_id or None)
    return 0


def _maybe_apply_snippet(event: Event, config: Config) -> Event:
    """Replace `event.message` with a head/tail slice of the last assistant turn.

    Applies to turn_complete AND idle_prompt events: in the common coalesce
    flow the turn_complete ping is cancelled by the follow-up idle_prompt, so
    idle_prompt is where the snippet actually needs to appear. Permission and
    elicitation already have meaningful bodies (tool_input / MCP message), so
    they're left alone.
    """
    if not config.summary.enabled:
        return event
    if event.kind not in ("turn_complete", "idle_prompt"):
        return event
    if event.transcript_path is None:
        return event
    text = transcript.read_last_assistant_text(event.transcript_path)
    if not text:
        return event
    snippet = transcript.head_tail_snippet(
        text,
        head=config.summary.head_chars,
        tail=config.summary.tail_chars,
    )
    if not snippet:
        return event
    return replace(event, message=snippet)


def _spawn_defer_child(
    config_path: Path | None, agent: str, session_id: str | None
) -> None:
    """POSIX double-fork a daemon grandchild that runs the defer dispatch.

    We tried `subprocess.Popen(start_new_session=True, stderr=<log>)` first and
    found the child was dying before it could write a single log line —
    despite stderr pointing at defer.log. Some combination of Claude Code's
    hook process lifecycle and subprocess re-exec was killing it.

    Double-fork sidesteps all of that: the grandchild inherits the parent's
    interpreter (no re-import, no `sys.executable` dependency), detaches from
    the session via `setsid`, and is reparented to init when the middle
    process exits — so Claude Code can't reap it. The parent reaps the middle
    process immediately so it doesn't become a zombie.
    """
    try:
        first = os.fork()
    except OSError as e:
        print(f"agent-notify: failed to fork defer child: {e}", file=sys.stderr)
        return
    if first != 0:
        # Parent: reap the middle child (which exits instantly) and return.
        try:
            os.waitpid(first, 0)
        except OSError:
            pass
        return
    # Middle process: detach from session, fork again, exit so the
    # grandchild is reparented to init (PID 1).
    try:
        os.setsid()
        second = os.fork()
        if second != 0:
            os._exit(0)
    except OSError:
        os._exit(0)
    # Grandchild: this is the daemon. Redirect stdio to /dev/null and stderr
    # to defer.log, then run the dispatch inline.
    try:
        _daemonize_fds()
        _run_defer_inline(config_path, agent, session_id)
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
    finally:
        os._exit(0)


def _daemonize_fds() -> None:
    """Redirect stdin/stdout/stderr for the daemon grandchild.

    stdin/stdout → /dev/null; stderr → defer.log (append). The redirection
    happens before any real work so a crash during load_config / import
    surfaces in the log rather than being swallowed."""
    log_path = _defer_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    devnull = os.open(os.devnull, os.O_RDWR)
    err_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(err_fd, 2)
    if devnull > 2:
        os.close(devnull)
    if err_fd > 2:
        os.close(err_fd)


def _run_defer_inline(
    config_path: Path | None, agent: str, session_id: str | None
) -> None:
    """Run the defer dispatch without re-parsing argv. Mirrors cmd_defer_dispatch.

    Kept separate so the double-fork grandchild doesn't pay import / argparse
    cost and so tests can exercise the same code path without actually forking.
    """
    _log_event(f"defer grandchild started agent={agent} sess={session_id}")
    config = load_config(config_path)
    if config.display.coalesce_window_seconds > 0:
        _sleep(config.display.coalesce_window_seconds)
    event = pending.claim(agent, session_id)
    if event is None:
        _log_event(f"defer grandchild exit: no pending sess={session_id}")
        return
    event = _maybe_apply_snippet(event, config)
    _log_event(f"defer grandchild dispatching sess={session_id} msg_len={len(event.message)}")
    _dispatch(event, config)
    _log_event(f"defer grandchild done sess={session_id}")


def _defer_log_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "coding-agent-notifier" / "defer.log"


def _log_event(msg: str) -> None:
    """Append a timestamped line to defer.log. Swallows all errors — a hook
    logging failure must never block the agent. Used to audit the defer
    pipeline (pending write, subprocess spawn, claim, dispatch)."""
    try:
        path = _defer_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] pid={os.getpid()} {msg}\n")
    except OSError:
        pass


def _is_duplicate(event: Event) -> bool:
    # Both agents can fire two hooks for the same logical event:
    #   - Claude Code: `PermissionRequest` + `Notification:permission_prompt`
    #                  (can be delayed up to ~60s when the user is slow to answer)
    #   - Codex:       `notify` (agent-turn-complete) + the `Stop` hook
    # Collapse pairs within a kind-specific TTL keyed on (agent, kind, session).
    # We deliberately ignore tool_name — the Notification payload doesn't carry
    # it so the keys would diverge otherwise.
    ttl = DEDUP_TTLS.get(event.kind)
    if ttl is None:
        return False
    key = dedup.dedup_key(event.agent, event.kind, event.session_id, None)
    return dedup.recently_seen(key, ttl=ttl)


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
    tool_input = None
    if args.kind == "permission":
        if args.dangerous:
            tool_input = {"command": "sudo rm -rf /tmp/agent-notify-test", "description": "synthetic dangerous command"}
        else:
            tool_input = {"command": "echo 'hello from agent-notify'"}
    event = Event(
        agent="claude-code",
        kind=args.kind,
        message="Synthetic test event from agent-notify",
        cwd=Path.cwd(),
        session_id="test1234",
        tool_name="Bash" if args.kind == "permission" else None,
        tool_input=tool_input,
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
    if config.routes:
        print(f"  routes:  {len(config.routes)} configured (strict: unmatched paths skipped)")
        matched = match_route(Path.cwd(), config)
        if matched is not None:
            print(f"    cwd={Path.cwd()} → matches {matched.cwd!r}")
        else:
            print(
                f"    cwd={Path.cwd()} → NO ROUTE MATCHES — notifications for this "
                f"path will be skipped. Add a `[[routes]]` entry or a `cwd = \"*\"` "
                f"catch-all if you want coverage here."
            )

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
    resolved = sinks_for(event.cwd, config)
    if resolved is None:
        print(
            f"agent-notify: no [[routes]] entry matches {event.cwd} — skipping "
            f"to avoid cross-project leakage. Add a route (or `cwd = \"*\"` "
            f"catch-all) to re-enable notifications for this path.",
            file=sys.stderr,
        )
        return
    slack_cfg, discord_cfg = resolved
    sinks: list[Sink] = []
    if slack_cfg.enabled:
        sinks.append(SlackSink(
            slack_cfg,
            tool_input_max_chars=config.tool_input_max_chars,
            verbosity=config.display.verbosity,
        ))
    if discord_cfg.enabled:
        sinks.append(DiscordSink(
            discord_cfg,
            tool_input_max_chars=config.tool_input_max_chars,
            verbosity=config.display.verbosity,
        ))
    for sink in sinks:
        try:
            sink.send(event)
        except SinkError as e:
            print(f"agent-notify: {sink.name} sink failed: {e}", file=sys.stderr)
