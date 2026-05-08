"""Integration-level tests: every state-writing code path produces 0600
files inside directories we control.

Rationale: unit tests catch the individual `write_secure` calls, but
only a walk of the real on-disk layout after exercising the hook
catches a callsite that forgot to use it.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from coding_agent_notifier import cli, paths, pending, pending_approvals


DIR_MODE = 0o700
FILE_MODE = 0o600


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _walk_and_assert(root: Path, *, expected_file_mode: int = FILE_MODE,
                    expected_dir_mode: int = DIR_MODE) -> None:
    if not root.exists():
        return
    for entry in root.rglob("*"):
        if entry.is_dir():
            assert _mode(entry) == expected_dir_mode, (
                f"directory {entry} has mode {oct(_mode(entry))}, expected {oct(expected_dir_mode)}"
            )
        elif entry.is_file():
            assert _mode(entry) == expected_file_mode, (
                f"file {entry} has mode {oct(_mode(entry))}, expected {oct(expected_file_mode)}"
            )


@pytest.fixture
def loose_umask():
    """Force a permissive umask so the test catches every case where we
    don't explicitly set mode at creation time. A `write_text` that
    relied on umask would slip through a default-0o022 umask test but
    visibly fail here."""
    old = os.umask(0o000)
    yield
    os.umask(old)


def test_pending_write_produces_0600(tmp_path, monkeypatch, loose_umask):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    from coding_agent_notifier.event import Event
    event = Event(
        agent="claude-code",
        kind="turn_complete",
        message="",
        cwd=Path("/tmp"),
        session_id="s-abc",
    )
    pending.write(event)
    root = paths.root()
    _walk_and_assert(root)
    # sanity: the actual pending file is there
    assert any(paths.pending_dir().iterdir())


def test_pending_approvals_create_produces_0600(tmp_path, monkeypatch, loose_umask):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    pending_approvals.create(
        "a",
        agent="claude-code",
        session_id="s",
        tool_name="Bash",
        tool_input={"command": "echo ok"},
    )
    pending_approvals.set_message_ref("a", "C1", "1.0")
    pending_approvals.resolve("a", "allow", actor="U1")
    # Everything in the approvals dir: files 0600, dirs 0700.
    # FIFOs are created with 0o600 directly and need special handling.
    root = paths.root()
    for entry in root.rglob("*"):
        if entry.is_dir():
            assert _mode(entry) == DIR_MODE, f"dir {entry} mode {oct(_mode(entry))}"
        elif entry.is_file() or (entry.exists() and not entry.is_symlink()):
            # Regular files + FIFOs both need 0600.
            assert _mode(entry) == FILE_MODE, f"{entry} mode {oct(_mode(entry))}"


def test_cmd_config_init_produces_0600(tmp_path, monkeypatch, loose_umask):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    rc = cli.main(["config", "init"])
    assert rc == 0
    assert _mode(paths.config_file()) == FILE_MODE
    # Parent dir 0700.
    assert _mode(paths.root()) == DIR_MODE


def test_log_event_produces_0600(tmp_path, monkeypatch, loose_umask):
    """When the user opts into [logging] level=basic, defer.log must still
    be created with 0600 perms — opting into logs is not opting out of
    the secure-write posture."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    # Default level is "off", so _log_event would no-op; write a minimal
    # config that enables it.
    cfg = tmp_path / "config.toml"
    cfg.write_text('gating = "always"\n\n[logging]\nlevel = "basic"\n')
    from coding_agent_notifier import cli
    cli._reset_log_level_cache()
    cli._log_event("test line")
    assert _mode(paths.defer_log()) == FILE_MODE
    assert _mode(paths.logs_dir()) == DIR_MODE
    cli._reset_log_level_cache()


def test_dedup_creates_0600_state(tmp_path, monkeypatch, loose_umask):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    from coding_agent_notifier import dedup
    dedup.recently_seen("test-key", ttl=60)
    assert paths.dedup_file().exists()
    assert _mode(paths.dedup_file()) == FILE_MODE


def test_install_writes_0600_settings(tmp_path, monkeypatch, loose_umask):
    from coding_agent_notifier import install as install_mod
    settings = tmp_path / "settings.json"
    install_mod.install_claude_code(settings)
    assert _mode(settings) == FILE_MODE


def test_install_slack_bot_writes_0600_plist(tmp_path, monkeypatch, loose_umask):
    from coding_agent_notifier import install as install_mod
    settings = tmp_path / "settings.json"
    la = tmp_path / "LaunchAgents"
    summary = install_mod.install_slack_bot(settings, launch_agents_dir=la)
    assert _mode(summary["plist_path"]) == FILE_MODE
    assert _mode(settings) == FILE_MODE


def test_pending_approvals_fifo_is_0600(tmp_path, monkeypatch, loose_umask):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    pending_approvals.create(
        "abc", agent="claude-code", session_id="s", tool_name="Bash",
    )
    fifo = paths.approvals_dir() / "abc.fifo"
    assert fifo.exists()
    assert _mode(fifo) == FILE_MODE
