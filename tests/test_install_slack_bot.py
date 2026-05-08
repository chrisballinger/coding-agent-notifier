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


def test_install_slack_bot_adds_posttooluse_hook(tmp_path: Path):
    """PostToolUse with matcher AskUserQuestion|ExitPlanMode is the back-fill
    path for cross-surface answers (TUI / Claude Code Remote on iOS) — must
    be installed alongside PermissionRequest, not require a separate command."""
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    summary = install.install_slack_bot(settings, launch_agents_dir=la_dir)
    data = json.loads(settings.read_text())
    assert "PostToolUse" in data["hooks"]
    entries = data["hooks"]["PostToolUse"]
    matchers = [e.get("matcher") for e in entries]
    assert "AskUserQuestion|ExitPlanMode" in matchers
    pt_cmds = [
        h["command"]
        for entry in entries
        for h in entry.get("hooks", [])
    ]
    assert any("agent-notify hook" in c for c in pt_cmds)
    assert "PostToolUse" in summary["claude_hooks_added"]


def test_install_slack_bot_posttooluse_idempotent(tmp_path: Path):
    """Running install_slack_bot twice must not duplicate the PostToolUse
    entry — that would fire our hook subprocess twice per AskUserQuestion."""
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    install.install_slack_bot(settings, launch_agents_dir=la_dir)
    install.install_slack_bot(settings, launch_agents_dir=la_dir)
    data = json.loads(settings.read_text())
    entries = data["hooks"]["PostToolUse"]
    ours = [e for e in entries if any(
        "agent-notify hook" in h.get("command", "") for h in e.get("hooks", [])
    )]
    assert len(ours) == 1


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
    # Throttle restart rate so a fast-failing daemon can't fork-bomb.
    assert "<key>ThrottleInterval</key>" in content
    assert "<integer>10</integer>" in content


def test_install_slack_bot_resolves_absolute_binary_path(tmp_path: Path, monkeypatch):
    """Default `agent_notify_bin="agent-notify"` is resolved to an absolute
    path via shutil.which. launchd's PATH doesn't include ~/.local/bin (the
    default uv-tool install location), so a bare program name caused
    EX_CONFIG and the daemon never started. The resolved absolute path
    keeps the plist robust to install location.
    """
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    fake_bin = tmp_path / "fake-bin" / "agent-notify"
    fake_bin.parent.mkdir()
    fake_bin.write_text("#!/bin/sh\nexec true\n")
    fake_bin.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda name: str(fake_bin) if name == "agent-notify" else None)

    install.install_slack_bot(settings, launch_agents_dir=la_dir)

    plist_text = (la_dir / f"{install.LAUNCHD_LABEL}.plist").read_text()
    # The absolute resolved path is in the ProgramArguments, not bare name.
    assert f"<string>{fake_bin}</string>" in plist_text


def test_install_slack_bot_keeps_explicit_bin_path_unresolved(tmp_path: Path):
    """When the caller passes an explicit `agent_notify_bin` (e.g. tests,
    bundled installs), don't shadow it with a `shutil.which` lookup."""
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    install.install_slack_bot(
        settings,
        launch_agents_dir=la_dir,
        agent_notify_bin="/some/explicit/agent-notify",
    )
    plist_text = (la_dir / f"{install.LAUNCHD_LABEL}.plist").read_text()
    assert "<string>/some/explicit/agent-notify</string>" in plist_text


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


def test_install_slack_bot_removes_legacy_plist(tmp_path: Path, monkeypatch):
    """A pre-rename plist (`com.chrisballinger.agent-notify-daemon.plist`)
    must be unlinked before the new one is written; otherwise two daemons
    fight for the same Socket Mode connection. We swallow launchctl errors
    so the migration works even when the old daemon isn't actually loaded."""
    settings = tmp_path / "settings.json"
    la_dir = tmp_path / "LaunchAgents"
    la_dir.mkdir()
    legacy_label = install._LEGACY_LAUNCHD_LABELS[0]
    legacy_plist = la_dir / f"{legacy_label}.plist"
    legacy_plist.write_text("<plist>old</plist>")
    # Stub launchctl so the test doesn't touch the real launchd.
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/launchctl" if name == "launchctl" else None)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        class _R:
            returncode = 1
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)

    summary = install.install_slack_bot(settings, launch_agents_dir=la_dir)

    assert not legacy_plist.exists(), "legacy plist must be removed"
    assert summary["legacy_plists_removed"] == [legacy_label]
    # bootout was attempted with the right label.
    assert any(legacy_label in " ".join(a) for a in calls)
    # The new plist is in place.
    assert (la_dir / f"{install.LAUNCHD_LABEL}.plist").exists()


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
