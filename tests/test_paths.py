from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_agent_notifier import paths


def test_root_uses_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    assert paths.root() == tmp_path


def test_root_default_is_dot_agent_notify(monkeypatch):
    monkeypatch.delenv("AGENT_NOTIFY_HOME", raising=False)
    assert paths.root() == Path.home() / ".agent-notify"


def test_root_expands_tilde(monkeypatch, tmp_path):
    # AGENT_NOTIFY_HOME=~/foo should expand, not produce a literal ~/foo
    monkeypatch.setenv("AGENT_NOTIFY_HOME", "~/agent-notify-test")
    assert str(paths.root()).startswith(str(Path.home()))


def test_subpaths_are_under_root(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    for p in (
        paths.config_file(),
        paths.state_dir(),
        paths.dedup_file(),
        paths.pending_dir(),
        paths.approvals_dir(),
        paths.logs_dir(),
        paths.defer_log(),
        paths.daemon_log(),
    ):
        assert str(p).startswith(str(tmp_path)), f"{p} is not under {tmp_path}"


def test_ensure_dir_creates_with_0700(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    d = paths.ensure_dir(tmp_path / "sub" / "nested")
    assert d.exists()
    assert (d.stat().st_mode & 0o777) == 0o700


def test_ensure_dir_tightens_existing_loose_mode(tmp_path):
    d = tmp_path / "loose"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    paths.ensure_dir(d)
    assert (d.stat().st_mode & 0o777) == 0o700


def test_write_secure_writes_0600_regardless_of_umask(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    # Force a dangerously-permissive umask; the write must still lock down.
    old = os.umask(0o000)
    try:
        paths.write_secure(tmp_path / "secret.txt", "hello")
    finally:
        os.umask(old)
    f = tmp_path / "secret.txt"
    assert f.read_text() == "hello"
    assert (f.stat().st_mode & 0o777) == 0o600


def test_write_secure_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    paths.write_secure(tmp_path / "b.bin", b"\x00\x01\x02")
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\x01\x02"
    assert ((tmp_path / "b.bin").stat().st_mode & 0o777) == 0o600


def test_write_secure_creates_parent_with_0700(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    target = tmp_path / "a" / "b" / "c.txt"
    paths.write_secure(target, "x")
    assert (target.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "a").stat().st_mode & 0o777) == 0o700
    assert ((tmp_path / "a" / "b").stat().st_mode & 0o777) == 0o700


def test_write_secure_is_atomic_overwrite(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    path = tmp_path / "x.txt"
    paths.write_secure(path, "v1")
    paths.write_secure(path, "v2")
    assert path.read_text() == "v2"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_open_append_secure_respects_0600(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    old = os.umask(0o000)
    try:
        log = tmp_path / "logs" / "x.log"
        with paths.open_append_secure(log) as f:
            f.write("first\n")
        with paths.open_append_secure(log) as f:
            f.write("second\n")
    finally:
        os.umask(old)
    assert log.read_text() == "first\nsecond\n"
    assert (log.stat().st_mode & 0o777) == 0o600


def test_tighten_is_noop_when_missing(tmp_path):
    paths.tighten(tmp_path / "missing.txt")  # should not raise
