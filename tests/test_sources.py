from __future__ import annotations

from pathlib import Path

from coding_agent_notifier.sources import claude_code, codex


def test_claude_notification_permission(load_fixture):
    ev = claude_code.parse(load_fixture("claude_notification_permission.json"))
    assert ev is not None
    assert ev.agent == "claude-code"
    assert ev.kind == "permission"
    assert ev.session_id == "abc123def456"
    assert ev.cwd == Path("/Users/example/src/myproj")
    assert "permission" in ev.message.lower()


def test_claude_notification_idle(load_fixture):
    ev = claude_code.parse(load_fixture("claude_notification_idle.json"))
    assert ev is not None
    assert ev.kind == "idle_prompt"


def test_claude_notification_elicitation(load_fixture):
    ev = claude_code.parse(load_fixture("claude_notification_elicitation.json"))
    assert ev is not None
    assert ev.kind == "elicitation"


def test_claude_notification_auth_success_skipped():
    payload = {
        "hook_event_name": "Notification",
        "notification_type": "auth_success",
        "cwd": "/tmp",
    }
    assert claude_code.parse(payload) is None


def test_claude_permission_request_with_bash(load_fixture):
    ev = claude_code.parse(load_fixture("claude_permission_request.json"))
    assert ev is not None
    assert ev.kind == "permission"
    assert ev.tool_name == "Bash"
    assert ev.tool_input == {
        "command": "rm -rf node_modules",
        "description": "Remove node_modules directory",
    }
    # Permission events should leave `message` empty — sink layout carries the
    # tool name as a field, so "Tool: X" duplication is the anti-pattern.
    assert ev.message == ""


def test_claude_permission_request_passes_through_arbitrary_tool():
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "CustomTool",
        "tool_input": {"target": "foo", "mode": "bar"},
        "cwd": "/tmp",
    }
    ev = claude_code.parse(payload)
    assert ev is not None
    assert ev.tool_input == {"target": "foo", "mode": "bar"}


def test_claude_permission_request_missing_tool_input():
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Something",
        "cwd": "/tmp",
    }
    ev = claude_code.parse(payload)
    assert ev is not None
    assert ev.tool_input is None


def test_claude_permission_request_non_dict_tool_input_discarded():
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Odd",
        "tool_input": "not a dict",
        "cwd": "/tmp",
    }
    ev = claude_code.parse(payload)
    assert ev is not None
    assert ev.tool_input is None


def test_claude_stop(load_fixture):
    ev = claude_code.parse(load_fixture("claude_stop.json"))
    assert ev is not None
    assert ev.kind == "turn_complete"


def test_claude_unknown_hook_returns_none():
    assert claude_code.parse({"hook_event_name": "SessionStart", "cwd": "/"}) is None


def test_claude_source_app_is_propagated(load_fixture):
    ev = claude_code.parse(load_fixture("claude_stop.json"), source_app="iTerm2")
    assert ev is not None
    assert ev.source_app == "iTerm2"


def test_codex_notify_turn_complete(load_fixture):
    ev = codex.parse(load_fixture("codex_notify_turn_complete.json"))
    assert ev is not None
    assert ev.agent == "codex"
    assert ev.kind == "turn_complete"
    assert "refactor" in ev.message
    assert ev.session_id == "turn-9f"


def test_codex_hooks_stop():
    payload = {"hook_event_name": "Stop", "cwd": "/p", "session_id": "s"}
    ev = codex.parse(payload)
    assert ev is not None
    assert ev.kind == "turn_complete"


def test_codex_hooks_permission(load_fixture):
    ev = codex.parse(load_fixture("codex_permission_request.json"))
    assert ev is not None
    assert ev.kind == "permission"
    assert ev.tool_name == "shell"
    assert ev.tool_input == {"command": "git push origin main"}
    assert ev.message == ""


def test_codex_unknown_returns_none():
    assert codex.parse({"type": "something-else", "cwd": "/"}) is None


def test_codex_permission_non_dict_tool_input_discarded():
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "weird",
        "tool_input": ["not a dict"],
        "cwd": "/",
    }
    ev = codex.parse(payload)
    assert ev is not None
    assert ev.tool_input is None
