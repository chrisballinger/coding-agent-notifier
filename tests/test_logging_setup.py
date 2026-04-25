from __future__ import annotations

import io
import logging
from pathlib import Path

from coding_agent_notifier import cli, logging_setup


def _our_handlers(root: logging.Logger):
    return [h for h in root.handlers if getattr(h, "_agent_notify", False)]


def test_default_configure_sets_warning_level_and_stderr_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    logging_setup.configure(debug=False)
    root = logging.getLogger()
    assert root.level == logging.WARNING
    handlers = _our_handlers(root)
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)


def test_debug_configure_sets_debug_level(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    logging_setup.configure(debug=True)
    assert logging.getLogger().level == logging.DEBUG


def test_daemon_configure_adds_rotating_file_handler(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    logging_setup.configure(debug=False, daemon=True)
    handlers = _our_handlers(logging.getLogger())
    # stderr + rotating file = 2 handlers we own.
    assert len(handlers) == 2
    file_handlers = [
        h for h in handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename).name == "daemon.log"


def test_configure_is_idempotent(tmp_path: Path, monkeypatch):
    """Re-calling configure must not stack our handlers."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    logging_setup.configure(debug=False, daemon=True)
    logging_setup.configure(debug=False, daemon=True)
    handlers = _our_handlers(logging.getLogger())
    assert len(handlers) == 2  # stderr + rotating file, not 4.


def test_configure_preserves_third_party_handlers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    third_party = logging.StreamHandler(io.StringIO())
    logging.getLogger().addHandler(third_party)
    try:
        logging_setup.configure(debug=False)
        assert third_party in logging.getLogger().handlers
    finally:
        logging.getLogger().removeHandler(third_party)


def test_cli_debug_flag_lowers_root_level(tmp_path: Path, monkeypatch, capsys):
    """The top-level `--debug` flag wires through to the root logger so a
    DEBUG-level log from any module surfaces on stderr."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(cli.macos, "idle_seconds", lambda: 0.0)
    monkeypatch.setattr(cli.macos, "frontmost_app", lambda: None)
    cli.main(["--config", str(cfg), "--debug", "doctor"])
    assert logging.getLogger().level == logging.DEBUG
