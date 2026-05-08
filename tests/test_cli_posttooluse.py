"""Tests for the PostToolUse back-fill path: when the user answers an
AskUserQuestion / ExitPlanMode prompt on a non-Slack surface (TUI, Claude
Code Remote on iOS), Claude Code fires PostToolUse and we update the
existing Slack message with the chosen answer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from coding_agent_notifier import cli, pending_approvals
from coding_agent_notifier.config import (
    Config,
    DisplayConfig,
    SlackConfig,
    SummaryConfig,
)


def _enabled_config(verbosity: str = "terse") -> Config:
    return Config(
        slack=SlackConfig(
            enabled=True,
            bot_token="xoxb-test",
            app_token="xapp-test",
            channel="C1",
            actionable_approvals=True,
            approver_user_ids=("U_OK",),
            approval_timeout_seconds=1.0,
        ),
        display=DisplayConfig(verbosity=verbosity),
        summary=SummaryConfig(enabled=False),
    )


def _seed_timed_out_record(*, tool_use_id: str, tool_name: str, tool_input: dict):
    """Create a pending approval already marked timed_out, mirroring what
    `cmd_permissionrequest` writes when its Slack wait elapsed."""
    approval_id = "test-approval-1"
    pending_approvals.create(
        approval_id,
        agent="claude-code",
        session_id="abc123",
        tool_name=tool_name,
        tool_input=tool_input,
        workspace="default",
        tool_use_id=tool_use_id,
    )
    pending_approvals.set_message_ref(approval_id, "C1", "1.001")
    pending_approvals.resolve(approval_id, "timed_out", actor="timeout")
    return approval_id


def _good_poster():
    calls: list[dict] = []

    def post(url, payload, *, headers=None, timeout=10.0):
        calls.append({"url": url, "payload": payload})
        return 200, json.dumps({"ok": True, "channel": "C1", "ts": "1.001"})

    post.calls = calls  # type: ignore[attr-defined]
    return post


def _make_args(tmp_path):
    """Replicate what argparse would build for a `hook` invocation."""
    args = argparse.Namespace()
    args.config = None
    args.cmd = "hook"
    args.source = "claude-code"
    args.force = False
    args.debug = False
    return args


def _write_config(tmp_path: Path, verbosity: str = "terse") -> Path:
    """Write a minimal config.toml that enables the actionable workspace.
    Routes use a wildcard so the cwd in the fixture matches without
    needing to know it ahead of time."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'''
gating = "always"

[display]
verbosity = "{verbosity}"

[slack.workspaces.default]
enabled = true
bot_token = "xoxb-test"
app_token = "xapp-test"
channel = "C1"
actionable_approvals = true
approver_user_ids = ["U_OK"]

[[routes]]
cwd = "*"
slack_workspace = "default"
''')
    return cfg_path


def test_posttooluse_ask_user_question_updates_slack(load_fixture, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg_path = _write_config(tmp_path)

    payload = load_fixture("post_tool_use_ask_user_question.json")
    approval_id = _seed_timed_out_record(
        tool_use_id=payload["tool_use_id"],
        tool_name="AskUserQuestion",
        tool_input=payload["tool_input"],
    )

    poster = _good_poster()
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json", poster,
    )

    args = _make_args(tmp_path)
    args.config = cfg_path
    rc = cli.cmd_posttooluse(payload, args)
    assert rc == 0

    # Slack chat.update was called with a resolved message body.
    update_calls = [c for c in poster.calls if "chat.update" in c["url"]]
    assert len(update_calls) == 1
    body = update_calls[0]["payload"]
    assert body["channel"] == "C1"
    assert body["ts"] == "1.001"
    # The chosen option label appears somewhere in the rendered body.
    rendered = json.dumps(body)
    assert "Per-repo files" in rendered

    # Record was upgraded from "timed_out" to "allow" with the answer
    # in selected_options. The "Per-repo files" option is index 1.
    rec = pending_approvals.read(approval_id)
    # Cleanup runs after a successful chat.update, so the record may be
    # gone — that's also a valid post-condition.
    if rec is not None:
        assert rec["decision"] == "allow"
        assert rec["actor"] == "external"
        assert rec["selected_options"] == {"0": 1}


def test_posttooluse_no_pending_record_is_noop(load_fixture, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg_path = _write_config(tmp_path)
    payload = load_fixture("post_tool_use_ask_user_question.json")
    poster = _good_poster()
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json", poster,
    )
    args = _make_args(tmp_path)
    args.config = cfg_path
    rc = cli.cmd_posttooluse(payload, args)
    assert rc == 0
    # No record → no chat.update.
    assert not poster.calls


def test_posttooluse_already_resolved_is_noop(load_fixture, tmp_path, monkeypatch):
    """If a Slack click resolved the approval before PostToolUse arrived,
    we don't double-update — Slack already shows the decision."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg_path = _write_config(tmp_path)
    payload = load_fixture("post_tool_use_ask_user_question.json")
    approval_id = pending_approvals.create(
        "raced-approval",
        agent="claude-code",
        session_id="abc123",
        tool_name="AskUserQuestion",
        tool_input=payload["tool_input"],
        workspace="default",
        tool_use_id=payload["tool_use_id"],
    )
    pending_approvals.set_message_ref("raced-approval", "C1", "1.001")
    pending_approvals.resolve("raced-approval", "allow", actor="U_OK", selected_option=0)

    poster = _good_poster()
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json", poster,
    )
    args = _make_args(tmp_path)
    args.config = cfg_path
    rc = cli.cmd_posttooluse(payload, args)
    assert rc == 0
    # Already-resolved record shouldn't be re-updated.
    assert not poster.calls


def test_posttooluse_minimal_verbosity_renders_generic_body(load_fixture, tmp_path, monkeypatch):
    """In minimal mode the per-question answer text is suppressed — the
    user has explicitly opted out of transmitting answer details to Slack
    for privacy."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg_path = _write_config(tmp_path, verbosity="minimal")
    payload = load_fixture("post_tool_use_ask_user_question.json")
    _seed_timed_out_record(
        tool_use_id=payload["tool_use_id"],
        tool_name="AskUserQuestion",
        tool_input=payload["tool_input"],
    )

    poster = _good_poster()
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json", poster,
    )
    args = _make_args(tmp_path)
    args.config = cfg_path
    rc = cli.cmd_posttooluse(payload, args)
    assert rc == 0
    update_calls = [c for c in poster.calls if "chat.update" in c["url"]]
    assert len(update_calls) == 1
    rendered = json.dumps(update_calls[0]["payload"])
    # Minimal mode should NOT leak the chosen option label.
    assert "Per-repo files" not in rendered


def test_posttooluse_unknown_tool_is_noop(tmp_path, monkeypatch):
    """We only register PostToolUse for AskUserQuestion / ExitPlanMode,
    but a careless settings.json edit could send us other tools — drop
    them silently."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg_path = _write_config(tmp_path)
    poster = _good_poster()
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json", poster,
    )
    args = _make_args(tmp_path)
    args.config = cfg_path
    rc = cli.cmd_posttooluse(
        {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "x"},
        args,
    )
    assert rc == 0
    assert not poster.calls


def test_posttooluse_exit_plan_mode_collapses_message(load_fixture, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg_path = _write_config(tmp_path)
    payload = load_fixture("post_tool_use_exit_plan_mode.json")
    _seed_timed_out_record(
        tool_use_id=payload["tool_use_id"],
        tool_name="ExitPlanMode",
        tool_input=payload["tool_input"],
    )
    poster = _good_poster()
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json", poster,
    )
    args = _make_args(tmp_path)
    args.config = cfg_path
    rc = cli.cmd_posttooluse(payload, args)
    assert rc == 0
    update_calls = [c for c in poster.calls if "chat.update" in c["url"]]
    assert len(update_calls) == 1
    rendered = json.dumps(update_calls[0]["payload"])
    # Approve verb + "another surface" attribution.
    assert "Approved" in rendered
    assert "another surface" in rendered


def test_posttooluse_exit_plan_mode_deny_signal(tmp_path, monkeypatch):
    """When tool_response signals the plan was rejected, render the deny
    state — not allow."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg_path = _write_config(tmp_path)
    _seed_timed_out_record(
        tool_use_id="toolu_deny",
        tool_name="ExitPlanMode",
        tool_input={"plan": "do stuff"},
    )
    poster = _good_poster()
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json", poster,
    )
    args = _make_args(tmp_path)
    args.config = cfg_path
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "abc123",
        "cwd": "/tmp",
        "tool_name": "ExitPlanMode",
        "tool_use_id": "toolu_deny",
        "tool_response": {"approved": False},
    }
    rc = cli.cmd_posttooluse(payload, args)
    assert rc == 0
    update_calls = [c for c in poster.calls if "chat.update" in c["url"]]
    assert len(update_calls) == 1
    rendered = json.dumps(update_calls[0]["payload"])
    assert "Denied" in rendered


def test_posttooluse_finds_record_via_session_fallback(tmp_path, monkeypatch):
    """Older approvals didn't store tool_use_id — fall back to
    (session_id, tool_name) so users mid-upgrade don't lose back-fill."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg_path = _write_config(tmp_path)
    # Record has no tool_use_id (passes None).
    pending_approvals.create(
        "old-style-approval",
        agent="claude-code",
        session_id="legacy-sess",
        tool_name="AskUserQuestion",
        tool_input={"questions": [{"question": "Q?", "options": [{"label": "A"}, {"label": "B"}]}]},
        workspace="default",
    )
    pending_approvals.set_message_ref("old-style-approval", "C1", "1.001")
    pending_approvals.resolve("old-style-approval", "timed_out", actor="timeout")

    poster = _good_poster()
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json", poster,
    )
    args = _make_args(tmp_path)
    args.config = cfg_path
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "legacy-sess",
        "cwd": "/tmp",
        "tool_name": "AskUserQuestion",
        "tool_response": {"answers": {"Q?": "A"}},
    }
    rc = cli.cmd_posttooluse(payload, args)
    assert rc == 0
    update_calls = [c for c in poster.calls if "chat.update" in c["url"]]
    assert len(update_calls) == 1
