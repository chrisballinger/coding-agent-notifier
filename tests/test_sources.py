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
    # auth_success isn't a kind we route on
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
    assert ev.tool_input_preview == "rm -rf node_modules"


def test_claude_permission_request_falls_back_to_description():
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Edit",
        "tool_input": {"description": "apply a patch"},
        "cwd": "/tmp",
    }
    ev = claude_code.parse(payload)
    assert ev is not None
    assert ev.tool_input_preview == "apply a patch"


def test_claude_permission_request_falls_back_to_json():
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "CustomTool",
        "tool_input": {"target": "foo", "mode": "bar"},
        "cwd": "/tmp",
    }
    ev = claude_code.parse(payload)
    assert ev is not None
    assert ev.tool_input_preview is not None
    assert "target" in ev.tool_input_preview


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
    assert ev.tool_input_preview == "git push origin main"


def test_codex_unknown_returns_none():
    assert codex.parse({"type": "something-else", "cwd": "/"}) is None


def test_codex_permission_fallback_json():
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "weird",
        "tool_input": {"x": 1},
        "cwd": "/",
    }
    ev = codex.parse(payload)
    assert ev is not None
    assert ev.tool_input_preview is not None
