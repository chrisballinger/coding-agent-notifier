from __future__ import annotations

import json
from pathlib import Path

from coding_agent_notifier import install


def test_claude_install_creates_settings(tmp_path: Path):
    settings = tmp_path / "settings.json"
    added = install.install_claude_code(settings)
    # PermissionRequest is intentionally NOT here — it's added only by the
    # Slack-bot install (with the long timeout for the blocking decision
    # round-trip). Base notifications for permission events come via the
    # Notification hook with matcher `permission_prompt`.
    assert set(added) == {"Notification", "Stop", "UserPromptSubmit"}
    data = json.loads(settings.read_text())
    assert "hooks" in data
    assert any(
        "agent-notify hook" in h["command"]
        for h in data["hooks"]["Notification"][0]["hooks"]
    )


def test_claude_install_is_idempotent(tmp_path: Path):
    settings = tmp_path / "settings.json"
    install.install_claude_code(settings)
    first = settings.read_text()
    added = install.install_claude_code(settings)
    assert added == []
    # structure unchanged apart from timestamp backup
    assert json.loads(settings.read_text()) == json.loads(first)


def test_claude_install_preserves_existing_hooks(tmp_path: Path):
    settings = tmp_path / "settings.json"
    existing = {
        "hooks": {
            "Notification": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "/bin/echo existing"}],
                }
            ]
        },
        "unrelated_top_level_key": 42,
    }
    settings.write_text(json.dumps(existing))
    install.install_claude_code(settings)
    data = json.loads(settings.read_text())
    assert data["unrelated_top_level_key"] == 42
    commands = [
        h["command"]
        for entry in data["hooks"]["Notification"]
        for h in entry["hooks"]
    ]
    assert "/bin/echo existing" in commands
    assert any("agent-notify hook" in c for c in commands)


def test_codex_install_creates_files(tmp_path: Path):
    config = tmp_path / "config.toml"
    hooks = tmp_path / "hooks.json"
    summary = install.install_codex(config, hooks)
    assert summary["config_updated"] is True
    assert set(summary["hooks_added"]) == {"Stop", "PermissionRequest"}
    assert "agent-notify" in config.read_text()
    hooks_data = json.loads(hooks.read_text())
    assert "PermissionRequest" in hooks_data["hooks"]


def test_codex_install_idempotent(tmp_path: Path):
    config = tmp_path / "config.toml"
    hooks = tmp_path / "hooks.json"
    install.install_codex(config, hooks)
    summary = install.install_codex(config, hooks)
    assert summary["hooks_added"] == []
    assert summary["config_updated"] is False


def test_codex_install_preserves_unrelated_config(tmp_path: Path):
    config = tmp_path / "config.toml"
    hooks = tmp_path / "hooks.json"
    config.write_text('model = "gpt-5"\n')
    install.install_codex(config, hooks)
    text = config.read_text()
    assert 'model = "gpt-5"' in text
    assert "notify" in text


def test_codex_install_flags_prior_notify(tmp_path: Path):
    config = tmp_path / "config.toml"
    hooks = tmp_path / "hooks.json"
    config.write_text('notify = ["/usr/bin/my-existing-notifier"]\n')
    summary = install.install_codex(config, hooks)
    assert "previous_notify" in summary
    assert summary["previous_notify"] == ["/usr/bin/my-existing-notifier"]


def test_merge_claude_hooks_skips_when_present():
    existing = {
        "hooks": {
            "Notification": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "agent-notify hook --source claude-code"}]}
            ]
        }
    }
    _, added = install.merge_claude_hooks(existing)
    assert "Notification" not in added
