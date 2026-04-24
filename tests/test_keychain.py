from __future__ import annotations

import subprocess

import pytest

from coding_agent_notifier import keychain


def _completed(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _force_macos(monkeypatch) -> None:
    monkeypatch.setattr(keychain, "is_macos", lambda: True)


def test_account_for_formats_workspace_and_field():
    assert keychain.account_for("home", "bot_token") == "home:bot_token"


# --- is_available ------------------------------------------------------


def test_is_available_false_on_non_macos(monkeypatch):
    monkeypatch.setattr(keychain, "is_macos", lambda: False)
    assert keychain.is_available() is False


def test_is_available_true_when_security_runs(monkeypatch):
    _force_macos(monkeypatch)
    monkeypatch.setattr(keychain.subprocess, "run", lambda *_a, **_k: _completed())
    assert keychain.is_available() is True


def test_is_available_false_when_security_missing(monkeypatch):
    _force_macos(monkeypatch)

    def boom(*_a, **_k):
        raise FileNotFoundError()

    monkeypatch.setattr(keychain.subprocess, "run", boom)
    assert keychain.is_available() is False


# --- read --------------------------------------------------------------


def test_read_returns_value(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **_k):
        captured["cmd"] = cmd
        return _completed(stdout="xoxb-real-token\n")

    _force_macos(monkeypatch)
    monkeypatch.setattr(keychain.subprocess, "run", fake_run)
    assert keychain.read("home:bot_token") == "xoxb-real-token"
    # Assert the argv is shaped as expected so a refactor doesn't silently
    # break the Keychain call.
    assert captured["cmd"][:2] == ["/usr/bin/security", "find-generic-password"]
    assert "agent-notify" in captured["cmd"]
    assert "home:bot_token" in captured["cmd"]
    assert "-w" in captured["cmd"]


def test_read_not_found_returns_none(monkeypatch):
    _force_macos(monkeypatch)
    monkeypatch.setattr(keychain.subprocess, "run", lambda *_a, **_k: _completed(rc=44))
    assert keychain.read("home:bot_token") is None


def test_read_other_nonzero_raises(monkeypatch):
    _force_macos(monkeypatch)
    monkeypatch.setattr(
        keychain.subprocess,
        "run",
        lambda *_a, **_k: _completed(rc=1, stderr="permission denied"),
    )
    with pytest.raises(keychain.KeychainError) as ei:
        keychain.read("home:bot_token")
    assert "exited 1" in str(ei.value)
    assert "permission denied" in str(ei.value)


def test_read_empty_stdout_with_zero_exit_raises(monkeypatch):
    # rc=0 with empty output shouldn't look like "not found" — surface it.
    _force_macos(monkeypatch)
    monkeypatch.setattr(keychain.subprocess, "run", lambda *_a, **_k: _completed(stdout=""))
    with pytest.raises(keychain.KeychainError):
        keychain.read("home:bot_token")


def test_read_missing_binary_raises(monkeypatch):
    _force_macos(monkeypatch)

    def boom(*_a, **_k):
        raise FileNotFoundError()

    monkeypatch.setattr(keychain.subprocess, "run", boom)
    with pytest.raises(keychain.KeychainError) as ei:
        keychain.read("home:bot_token")
    assert "not found" in str(ei.value)


def test_read_timeout_raises(monkeypatch):
    _force_macos(monkeypatch)

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="security", timeout=5)

    monkeypatch.setattr(keychain.subprocess, "run", boom)
    with pytest.raises(keychain.KeychainError) as ei:
        keychain.read("home:bot_token")
    assert "timed out" in str(ei.value)


def test_read_on_non_macos_raises(monkeypatch):
    monkeypatch.setattr(keychain, "is_macos", lambda: False)
    with pytest.raises(keychain.KeychainError):
        keychain.read("home:bot_token")


# --- write -------------------------------------------------------------


def test_write_success(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **_k):
        captured["cmd"] = cmd
        return _completed()

    _force_macos(monkeypatch)
    monkeypatch.setattr(keychain.subprocess, "run", fake_run)
    keychain.write("home:bot_token", "xoxb-secret")
    assert captured["cmd"][:2] == ["/usr/bin/security", "add-generic-password"]
    assert "-U" in captured["cmd"]
    assert "xoxb-secret" in captured["cmd"]
    assert "home:bot_token" in captured["cmd"]


def test_write_rejects_empty_value(monkeypatch):
    _force_macos(monkeypatch)
    with pytest.raises(ValueError):
        keychain.write("home:bot_token", "")


def test_write_failure_raises(monkeypatch):
    _force_macos(monkeypatch)
    monkeypatch.setattr(
        keychain.subprocess,
        "run",
        lambda *_a, **_k: _completed(rc=1, stderr="user interaction required"),
    )
    with pytest.raises(keychain.KeychainError) as ei:
        keychain.write("home:bot_token", "xoxb-x")
    assert "exited 1" in str(ei.value)
    assert "user interaction required" in str(ei.value)


def test_write_non_macos_raises(monkeypatch):
    monkeypatch.setattr(keychain, "is_macos", lambda: False)
    with pytest.raises(keychain.KeychainError):
        keychain.write("home:bot_token", "xoxb-x")


def test_write_missing_binary_raises(monkeypatch):
    _force_macos(monkeypatch)

    def boom(*_a, **_k):
        raise FileNotFoundError()

    monkeypatch.setattr(keychain.subprocess, "run", boom)
    with pytest.raises(keychain.KeychainError):
        keychain.write("home:bot_token", "xoxb-x")


# --- delete ------------------------------------------------------------


def test_delete_success_returns_true(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **_k):
        captured["cmd"] = cmd
        return _completed()

    _force_macos(monkeypatch)
    monkeypatch.setattr(keychain.subprocess, "run", fake_run)
    assert keychain.delete("home:bot_token") is True
    assert captured["cmd"][:2] == ["/usr/bin/security", "delete-generic-password"]


def test_delete_missing_returns_false(monkeypatch):
    _force_macos(monkeypatch)
    monkeypatch.setattr(keychain.subprocess, "run", lambda *_a, **_k: _completed(rc=44))
    assert keychain.delete("home:bot_token") is False


def test_delete_other_failure_raises(monkeypatch):
    _force_macos(monkeypatch)
    monkeypatch.setattr(
        keychain.subprocess,
        "run",
        lambda *_a, **_k: _completed(rc=1, stderr="locked keychain"),
    )
    with pytest.raises(keychain.KeychainError) as ei:
        keychain.delete("home:bot_token")
    assert "locked keychain" in str(ei.value)


def test_delete_non_macos_raises(monkeypatch):
    monkeypatch.setattr(keychain, "is_macos", lambda: False)
    with pytest.raises(keychain.KeychainError):
        keychain.delete("home:bot_token")
