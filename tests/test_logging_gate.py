"""Tests for the `[logging] level` config gate.

Default is `off` — defer.log is never created. `basic` writes audit lines
without sensitive metadata. `verbose` writes everything including the
`_emit_decision` JSON capture.

Memory cache (`cli._cached_log_level`) needs explicit reset between cases
since the cache is process-lifetime — production processes are short-lived
hooks/daemon, but the test process spans many cases."""
from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent_notifier import cli, paths


def _write_config(home: Path, level: str | None) -> None:
    cfg = home / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if level is None:
        # No [logging] section — should default to "off".
        cfg.write_text(
            'gating = "always"\n'
        )
    else:
        cfg.write_text(
            f'gating = "always"\n\n'
            f'[logging]\nlevel = "{level}"\n'
        )


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test starts with a fresh log-level cache so the previous
    test's config doesn't bleed in."""
    cli._reset_log_level_cache()
    yield
    cli._reset_log_level_cache()


def test_level_default_off_no_writes(tmp_path, monkeypatch):
    """No [logging] section means default off — calling _log_event must
    never create defer.log. This is the security-by-default contract."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    _write_config(tmp_path, level=None)
    cli._log_event("should not be written")
    cli._log_event_verbose("also should not be written")
    assert not paths.defer_log().exists()


def test_level_off_explicit_no_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    _write_config(tmp_path, level="off")
    cli._log_event("nope")
    cli._log_event_verbose("also nope")
    assert not paths.defer_log().exists()


def test_level_basic_writes_event_log(tmp_path, monkeypatch):
    """`basic` enables `_log_event` (audit-style lines) but suppresses
    `_log_event_verbose` (sensitive metadata). The split exists so a user
    can opt into 'did the hook fire?' debugging without surfacing channel
    IDs / freeform answer keys to anyone they later share defer.log with."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    _write_config(tmp_path, level="basic")
    cli._log_event("event-line")
    cli._log_event_verbose("verbose-line should be suppressed")
    text = paths.defer_log().read_text()
    assert "event-line" in text
    assert "verbose-line" not in text


def test_level_verbose_writes_both(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    _write_config(tmp_path, level="verbose")
    cli._log_event("event-line")
    cli._log_event_verbose("verbose-line")
    text = paths.defer_log().read_text()
    assert "event-line" in text
    assert "verbose-line" in text


def test_invalid_level_raises_config_error(tmp_path, monkeypatch):
    """A typo in the enum should fail loudly at config load — never
    silently fall through to a more-permissive level."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    _write_config(tmp_path, level="chatty")
    from coding_agent_notifier import config as cfg_mod
    with pytest.raises(cfg_mod.ConfigError):
        cfg_mod.load_config(None)


def test_defer_log_rotates_at_5mb(tmp_path, monkeypatch):
    """Bounded retention: with verbose on, the log file rolls over rather
    than growing unbounded. Verifies the user can leave verbose enabled
    on a long debug session without filling the disk."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    _write_config(tmp_path, level="verbose")
    log = paths.defer_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    # Pre-fill the file to just over the cap. Next _log_event call should
    # rename it to defer.log.1 and start fresh.
    log.write_bytes(b"x" * (cli._DEFER_LOG_MAX_BYTES + 1))
    cli._log_event("post-rotation")
    assert (log.parent / "defer.log.1").exists()
    # The rolled-over file holds the pre-rotation content (the x's).
    backup = (log.parent / "defer.log.1").read_bytes()
    assert backup.startswith(b"x")
    # The fresh file holds only the post-rotation line.
    fresh = log.read_text()
    assert "post-rotation" in fresh
    assert "x" * 100 not in fresh  # didn't carry over the pre-rotation bulk


def test_emit_decision_captures_json_at_verbose(tmp_path, monkeypatch):
    """The diagnostic capture exists for upstream-bug-report repros: at
    verbose, every `_emit_decision` writes its literal JSON to defer.log
    so a user investigating a render crash can paste the bytes."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    _write_config(tmp_path, level="verbose")
    import io
    buf = io.StringIO()
    cli._emit_decision(
        buf, "allow",
        updated_input={"answers": {"Pick one": "Option A"}},
    )
    text = paths.defer_log().read_text()
    assert "emit decision=allow" in text
    # The captured JSON is the literal bytes we wrote to stdout.
    assert "Option A" in text


def test_emit_decision_does_not_capture_at_basic(tmp_path, monkeypatch):
    """At `basic`, the JSON capture must NOT fire — option labels and
    question text are sensitive."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    _write_config(tmp_path, level="basic")
    import io
    buf = io.StringIO()
    cli._emit_decision(
        buf, "allow",
        updated_input={"answers": {"Pick one": "Option A"}},
    )
    if paths.defer_log().exists():
        text = paths.defer_log().read_text()
        assert "emit decision" not in text
        assert "Option A" not in text
