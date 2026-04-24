from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path

import pytest

from coding_agent_notifier import cli


def test_install_slack_bot_subcommand(monkeypatch, capsys):
    captured: dict = {}

    def fake(*, install_plist=True, **kw):
        captured["install_plist"] = install_plist
        return {
            "claude_hooks_added": ["PreToolUse"],
            "plist_path": Path("/tmp/fake.plist"),
            "plist_written": True,
        }

    monkeypatch.setattr("coding_agent_notifier.cli.install.install_slack_bot", fake)
    rc = cli.main(["install", "slack-bot"])
    assert rc == 0
    assert captured["install_plist"] is True
    out = capsys.readouterr()
    assert "PreToolUse" in out.out
    assert "launchctl load" in out.out
    # Instructions for next steps go to stderr. Point users at the wizard
    # rather than the legacy env-var dance.
    assert "agent-notify slack add" in out.err


def test_install_slack_bot_no_launchd(monkeypatch):
    captured: dict = {}

    def fake(*, install_plist=True, **kw):
        captured["install_plist"] = install_plist
        return {"claude_hooks_added": [], "plist_path": None, "plist_written": False}

    monkeypatch.setattr("coding_agent_notifier.cli.install.install_slack_bot", fake)
    rc = cli.main(["install", "slack-bot", "--no-launchd"])
    assert rc == 0
    assert captured["install_plist"] is False


def test_install_slack_bot_already_installed(monkeypatch, capsys):
    def fake(**kw):
        return {
            "claude_hooks_added": [],
            "plist_path": Path("/tmp/exists.plist"),
            "plist_written": False,
        }
    monkeypatch.setattr("coding_agent_notifier.cli.install.install_slack_bot", fake)
    rc = cli.main(["install", "slack-bot"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already installed" in out
    assert "already up to date" in out


def test_daemon_subcommand_calls_run_daemon(monkeypatch):
    called: dict = {}

    def fake_run(config, *, stop_event=None):
        called["ran"] = True
        called["slack_enabled"] = config.slack.enabled

    monkeypatch.setattr("coding_agent_notifier.slack_socket.run_daemon", fake_run)
    rc = cli.main(["daemon"])
    assert rc == 0
    assert called["ran"] is True


def test_cmd_hook_routes_pretooluse_to_approval_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    # Fake the heavy call — we just want to confirm the dispatch happens.
    called: dict = {}

    def fake_pretooluse(payload, config, **kw):
        called["payload"] = payload
        called["actionable"] = config.slack.actionable_approvals
        # Emit a valid deny JSON so cmd_hook sees a clean return.
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
            }
        }))
        return 0

    monkeypatch.setattr(cli, "cmd_pretooluse", fake_pretooluse)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": str(tmp_path),
    })))
    rc = cli.main(["hook", "--source", "claude-code"])
    assert rc == 0
    assert called["payload"]["tool_name"] == "Bash"


def test_cmd_hook_pretooluse_ignored_for_codex(monkeypatch, tmp_path):
    """PreToolUse is a Claude Code concept — the codex source shouldn't
    accidentally trigger the approval flow if codex ever emits a payload
    with that hook name."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    sentinel = {"called": False}

    def fake_pretooluse(*a, **kw):
        sentinel["called"] = True
        return 0

    monkeypatch.setattr(cli, "cmd_pretooluse", fake_pretooluse)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
    })))
    rc = cli.main(["hook", "--source", "codex"])
    assert rc == 0
    assert sentinel["called"] is False


def test_daemon_fails_loudly_when_slack_sdk_missing(monkeypatch):
    """If slack-sdk isn't installed, the daemon must raise with a clear
    install-command message rather than crashing on ImportError."""
    from coding_agent_notifier import slack_socket
    from coding_agent_notifier.config import Config, SlackConfig

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *a, **kw):
        if name == "slack_sdk" or name.startswith("slack_sdk."):
            raise ImportError(f"no module named {name}")
        return real_import(name, *a, **kw)

    if isinstance(__builtins__, dict):
        monkeypatch.setitem(__builtins__, "__import__", fake_import)
    else:
        monkeypatch.setattr(__builtins__, "__import__", fake_import)

    config = Config(slack=SlackConfig(
        enabled=True, bot_token="xoxb", app_token="xapp",
        interactive=True, actionable_approvals=True,
    ))
    with pytest.raises(RuntimeError, match="slack-sdk"):
        slack_socket.run_daemon(config)
