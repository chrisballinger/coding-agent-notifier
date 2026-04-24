from __future__ import annotations

import json
from pathlib import Path

from coding_agent_notifier import install


def test_install_slack_bot_adds_pretooluse_hook(tmp_path: Path):
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    summary = install.install_slack_bot(settings, launch_agents_dir=la_dir)
    data = json.loads(settings.read_text())
    assert "PreToolUse" in data["hooks"]
    pre_cmds = [
        h["command"]
        for entry in data["hooks"]["PreToolUse"]
        for h in entry["hooks"]
    ]
    assert any("agent-notify hook" in c for c in pre_cmds)
    # Also installs base hooks (Notification, Stop, etc.) for a fresh user.
    assert "Notification" in data["hooks"]
    assert "Stop" in data["hooks"]
    assert "PreToolUse" in summary["claude_hooks_added"]


def test_install_slack_bot_sets_long_timeout_on_pretooluse(tmp_path: Path):
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    install.install_slack_bot(settings, launch_agents_dir=la_dir)
    data = json.loads(settings.read_text())
    hooks = data["hooks"]["PreToolUse"][0]["hooks"]
    # Lock-screen round-trip budget: 10 minutes.
    assert any(h.get("timeout", 0) >= 300 for h in hooks)


def test_install_slack_bot_writes_launchd_plist(tmp_path: Path):
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    summary = install.install_slack_bot(settings, launch_agents_dir=la_dir)
    assert summary["plist_written"] is True
    plist = summary["plist_path"]
    assert plist.exists()
    content = plist.read_text()
    assert "agent-notify" in content
    assert "<string>daemon</string>" in content
    # Supervised: restarts on crash, starts on login.
    assert "<key>KeepAlive</key>" in content
    assert "<key>RunAtLoad</key>" in content


def test_install_slack_bot_is_idempotent(tmp_path: Path):
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    install.install_slack_bot(settings, launch_agents_dir=la_dir)
    first = settings.read_text()
    summary = install.install_slack_bot(settings, launch_agents_dir=la_dir)
    assert summary["claude_hooks_added"] == []
    assert summary["plist_written"] is False
    # Identity check on settings (modulo backup timestamp which is a separate file).
    assert settings.read_text() == first


def test_install_slack_bot_no_launchd(tmp_path: Path):
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    summary = install.install_slack_bot(settings, launch_agents_dir=la_dir, install_plist=False)
    assert summary["plist_path"] is None
    assert summary["plist_written"] is False
    assert not la_dir.exists() or not list(la_dir.iterdir())


def test_install_slack_bot_preserves_existing_claude_hooks(tmp_path: Path):
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    existing = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "/bin/echo preexisting"}],
                }
            ],
        },
        "otherKey": 7,
    }
    settings.write_text(json.dumps(existing))
    install.install_slack_bot(settings, launch_agents_dir=la_dir)
    data = json.loads(settings.read_text())
    assert data["otherKey"] == 7
    cmds = [
        h["command"]
        for entry in data["hooks"]["PreToolUse"]
        for h in entry["hooks"]
    ]
    assert "/bin/echo preexisting" in cmds
    assert any("agent-notify hook" in c for c in cmds)
