from __future__ import annotations

import json
from pathlib import Path

from coding_agent_notifier import install


def test_install_slack_bot_adds_permissionrequest_hook(tmp_path: Path):
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    summary = install.install_slack_bot(settings, launch_agents_dir=la_dir)
    data = json.loads(settings.read_text())
    assert "PermissionRequest" in data["hooks"]
    pr_cmds = [
        h["command"]
        for entry in data["hooks"]["PermissionRequest"]
        for h in entry["hooks"]
    ]
    assert any("agent-notify hook" in c for c in pr_cmds)
    # Also installs base hooks (Notification, Stop, etc.) for a fresh user.
    assert "Notification" in data["hooks"]
    assert "Stop" in data["hooks"]
    assert "PermissionRequest" in summary["claude_hooks_added"]


def test_install_slack_bot_sets_long_timeout_on_permissionrequest(tmp_path: Path):
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    install.install_slack_bot(settings, launch_agents_dir=la_dir)
    data = json.loads(settings.read_text())
    hooks = data["hooks"]["PermissionRequest"][0]["hooks"]
    # Lock-screen round-trip budget: 10 minutes.
    assert any(h.get("timeout", 0) >= 300 for h in hooks)


def test_install_slack_bot_migrates_legacy_pretooluse(tmp_path: Path):
    """Phase 1 used a PreToolUse hook for actionable approvals. The migration
    must remove our legacy PreToolUse entry while installing the new
    PermissionRequest entry — leaving any third-party PreToolUse entries
    intact."""
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    legacy = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "agent-notify hook --source claude-code",
                            "timeout": 600,
                        }
                    ],
                },
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "/usr/local/bin/lint-bash"}],
                },
            ],
        },
    }
    settings.write_text(json.dumps(legacy))
    install.install_slack_bot(settings, launch_agents_dir=la_dir)
    data = json.loads(settings.read_text())
    # Our PreToolUse entry is gone …
    pre = data["hooks"].get("PreToolUse", [])
    pre_cmds = [h["command"] for entry in pre for h in entry["hooks"]]
    assert all("agent-notify hook" not in c for c in pre_cmds)
    # … but the third-party PreToolUse entry survived.
    assert any(c == "/usr/local/bin/lint-bash" for c in pre_cmds)
    # And PermissionRequest is now installed.
    assert "PermissionRequest" in data["hooks"]


def test_install_slack_bot_drops_pretooluse_key_when_only_ours(tmp_path: Path):
    """If the legacy PreToolUse entry was the only one, removing it should
    drop the key entirely rather than leaving an empty array."""
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    legacy = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "agent-notify hook --source claude-code",
                            "timeout": 600,
                        }
                    ],
                },
            ],
        },
    }
    settings.write_text(json.dumps(legacy))
    install.install_slack_bot(settings, launch_agents_dir=la_dir)
    data = json.loads(settings.read_text())
    assert "PreToolUse" not in data["hooks"]


def test_install_slack_bot_upgrades_old_permissionrequest_no_timeout(tmp_path: Path):
    """An older base install wrote PermissionRequest without a timeout (it
    was a notification hook then). Slack-bot install must upgrade that
    entry to the timeout-bearing canonical form."""
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    legacy = {
        "hooks": {
            "PermissionRequest": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "agent-notify hook --source claude-code",
                        }
                    ],
                },
            ],
        },
    }
    settings.write_text(json.dumps(legacy))
    install.install_slack_bot(settings, launch_agents_dir=la_dir)
    data = json.loads(settings.read_text())
    pr = data["hooks"]["PermissionRequest"]
    pr_hooks = [h for entry in pr for h in entry["hooks"]]
    # Exactly one of our entries, and it now carries the timeout.
    ours = [h for h in pr_hooks if "agent-notify hook" in h.get("command", "")]
    assert len(ours) == 1
    assert ours[0].get("timeout", 0) >= 300


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
    # Third-party PreToolUse entry is untouched (we only remove ours).
    pre_cmds = [
        h["command"]
        for entry in data["hooks"]["PreToolUse"]
        for h in entry["hooks"]
    ]
    assert "/bin/echo preexisting" in pre_cmds
    # Our blocking entry now lives under PermissionRequest, not PreToolUse.
    pr_cmds = [
        h["command"]
        for entry in data["hooks"]["PermissionRequest"]
        for h in entry["hooks"]
    ]
    assert any("agent-notify hook" in c for c in pr_cmds)
