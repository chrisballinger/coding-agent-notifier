from __future__ import annotations

import subprocess

import pytest

from coding_agent_notifier import macos


def test_term_program_to_app_known_values():
    assert macos.term_program_to_app("iTerm.app") == "iTerm2"
    assert macos.term_program_to_app("Apple_Terminal") == "Terminal"
    assert macos.term_program_to_app("vscode") == "Code"


def test_term_program_to_app_unknown_falls_through():
    assert macos.term_program_to_app("CustomShell.app") == "CustomShell.app"


def test_term_program_to_app_none():
    assert macos.term_program_to_app(None) is None


def test_term_program_to_app_empty_fallthrough():
    # tmux is mapped to empty string → None via `or`
    assert macos.term_program_to_app("tmux") == "tmux"


def test_idle_seconds_on_non_macos(monkeypatch):
    monkeypatch.setattr(macos, "is_macos", lambda: False)
    assert macos.idle_seconds() is None


def test_idle_seconds_parses_ioreg_output(monkeypatch):
    sample = '    "HIDIdleTime" = 2500000000\n    "OtherKey" = 1\n'

    def fake_run(*_a, **_k):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=sample, stderr="")

    monkeypatch.setattr(macos, "is_macos", lambda: True)
    monkeypatch.setattr(macos.subprocess, "run", fake_run)
    assert macos.idle_seconds() == pytest.approx(2.5)


def test_idle_seconds_no_key(monkeypatch):
    def fake_run(*_a, **_k):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="nothing\n", stderr="")

    monkeypatch.setattr(macos, "is_macos", lambda: True)
    monkeypatch.setattr(macos.subprocess, "run", fake_run)
    assert macos.idle_seconds() is None


def test_idle_seconds_handles_subprocess_error(monkeypatch):
    def fake_run(*_a, **_k):
        raise FileNotFoundError()

    monkeypatch.setattr(macos, "is_macos", lambda: True)
    monkeypatch.setattr(macos.subprocess, "run", fake_run)
    assert macos.idle_seconds() is None


def test_idle_seconds_bad_number(monkeypatch):
    def fake_run(*_a, **_k):
        return subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout='    "HIDIdleTime" = notanumber\n', stderr="")

    monkeypatch.setattr(macos, "is_macos", lambda: True)
    monkeypatch.setattr(macos.subprocess, "run", fake_run)
    assert macos.idle_seconds() is None


def test_frontmost_app_non_macos(monkeypatch):
    monkeypatch.setattr(macos, "is_macos", lambda: False)
    assert macos.frontmost_app() is None


def test_frontmost_app_returns_name(monkeypatch):
    def fake_run(*_a, **_k):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="iTerm2\n", stderr="")
    monkeypatch.setattr(macos, "is_macos", lambda: True)
    monkeypatch.setattr(macos.subprocess, "run", fake_run)
    assert macos.frontmost_app() == "iTerm2"


def test_frontmost_app_empty_returns_none(monkeypatch):
    def fake_run(*_a, **_k):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="\n", stderr="")
    monkeypatch.setattr(macos, "is_macos", lambda: True)
    monkeypatch.setattr(macos.subprocess, "run", fake_run)
    assert macos.frontmost_app() is None


def test_frontmost_app_handles_error(monkeypatch):
    def fake_run(*_a, **_k):
        raise subprocess.CalledProcessError(1, ["osascript"])
    monkeypatch.setattr(macos, "is_macos", lambda: True)
    monkeypatch.setattr(macos.subprocess, "run", fake_run)
    assert macos.frontmost_app() is None
