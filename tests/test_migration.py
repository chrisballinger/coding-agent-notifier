from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from coding_agent_notifier import paths


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """Point every HOME-based path at tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path / ".agent-notify"))
    # paths module reads legacy dirs from Path.home() at import time — but they
    # are module-level constants, so we need to patch those.
    monkeypatch.setattr(paths, "_LEGACY_CONFIG_DIR", tmp_path / ".config" / "coding-agent-notifier")
    monkeypatch.setattr(paths, "_LEGACY_CACHE_DIR", tmp_path / ".cache" / "coding-agent-notifier")
    return tmp_path


def test_no_legacy_no_op(isolated_home):
    stderr = io.StringIO()
    moved = paths.migrate_legacy_state(stderr=stderr)
    assert moved == []
    assert stderr.getvalue() == ""


def test_migrates_config(isolated_home):
    legacy = paths._LEGACY_CONFIG_DIR
    legacy.mkdir(parents=True)
    (legacy / "config.toml").write_text('idle_threshold_seconds = 90\n')

    stderr = io.StringIO()
    moved = paths.migrate_legacy_state(stderr=stderr)
    assert any("config.toml" in m for m in moved)
    new_config = paths.config_file()
    assert new_config.exists()
    assert "90" in new_config.read_text()
    assert (new_config.stat().st_mode & 0o777) == 0o600
    assert "migrated legacy state" in stderr.getvalue()


def test_migrates_dedup_and_pending(isolated_home):
    legacy = paths._LEGACY_CACHE_DIR
    legacy.mkdir(parents=True)
    (legacy / "dedup.json").write_text(json.dumps({"k": 1.0}))
    (legacy / "pending").mkdir()
    (legacy / "pending" / "claude-code-x.json").write_text(
        json.dumps({"agent": "claude-code", "kind": "turn_complete",
                    "message": "", "cwd": "/tmp"})
    )

    stderr = io.StringIO()
    paths.migrate_legacy_state(stderr=stderr)

    assert paths.dedup_file().exists()
    assert (paths.pending_dir() / "claude-code-x.json").exists()
    # Everything 0600.
    assert (paths.dedup_file().stat().st_mode & 0o777) == 0o600
    assert ((paths.pending_dir() / "claude-code-x.json").stat().st_mode & 0o777) == 0o600
    # Dirs 0700.
    assert (paths.state_dir().stat().st_mode & 0o777) == 0o700
    assert (paths.pending_dir().stat().st_mode & 0o777) == 0o700


def test_renames_pending_approvals_to_approvals(isolated_home):
    legacy = paths._LEGACY_CACHE_DIR
    (legacy / "pending_approvals").mkdir(parents=True)
    (legacy / "pending_approvals" / "abc.json").write_text(json.dumps(
        {"approval_id": "abc", "agent": "claude-code", "decision": None,
         "created_at": 1.0}
    ))

    paths.migrate_legacy_state(stderr=io.StringIO())

    assert (paths.approvals_dir() / "abc.json").exists()
    # Old dir stays put for the user to inspect.
    assert (legacy / "pending_approvals").exists()


def test_migrates_logs(isolated_home):
    legacy = paths._LEGACY_CACHE_DIR
    legacy.mkdir(parents=True)
    (legacy / "defer.log").write_text("[timestamp] test line\n")
    (legacy / "daemon.log").write_text("connected\n")

    paths.migrate_legacy_state(stderr=io.StringIO())

    assert paths.defer_log().exists()
    assert paths.daemon_log().exists()
    assert (paths.defer_log().stat().st_mode & 0o777) == 0o600


def test_idempotent(isolated_home):
    legacy = paths._LEGACY_CONFIG_DIR
    legacy.mkdir(parents=True)
    (legacy / "config.toml").write_text("gating = \"always\"\n")

    paths.migrate_legacy_state(stderr=io.StringIO())
    # Second run: new dir now has content, so migration should report no moves.
    stderr2 = io.StringIO()
    moved2 = paths.migrate_legacy_state(stderr=stderr2)
    assert moved2 == []
    assert stderr2.getvalue() == ""


def test_does_not_overwrite_existing_new_config(isolated_home):
    # User may have manually created a new-layout config before migration —
    # we must not clobber it.
    paths.ensure_dir(paths.config_file().parent)
    paths.write_secure(paths.config_file(), 'gating = "always"\n')
    legacy = paths._LEGACY_CONFIG_DIR
    legacy.mkdir(parents=True)
    (legacy / "config.toml").write_text('gating = "never"\n')

    paths.migrate_legacy_state(stderr=io.StringIO())
    assert "always" in paths.config_file().read_text()


def test_leaves_legacy_in_place(isolated_home):
    legacy = paths._LEGACY_CONFIG_DIR
    legacy.mkdir(parents=True)
    (legacy / "config.toml").write_text('gating = "always"\n')
    paths.migrate_legacy_state(stderr=io.StringIO())
    assert (legacy / "config.toml").exists()  # still there for user to verify
