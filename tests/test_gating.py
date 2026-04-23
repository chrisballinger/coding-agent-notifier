from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent_notifier.config import Config, EventConfig
from coding_agent_notifier.event import Event
from coding_agent_notifier.gating import SystemState, should_send


def _event(kind="permission", source_app="iTerm2"):
    return Event(
        agent="claude-code",
        kind=kind,
        message="m",
        cwd=Path("/x"),
        source_app=source_app,
    )


def _config(**kw):
    return Config(**kw)


def test_disabled_event_never_sends():
    cfg = _config(events={"permission": EventConfig(enabled=False)})
    state = SystemState(idle_seconds=9999, frontmost_app="Safari")
    assert should_send(_event(), cfg, state) is False


def test_always_mode_sends_regardless_of_state():
    cfg = _config(gating="always")
    state = SystemState(idle_seconds=0, frontmost_app="iTerm2")
    assert should_send(_event(source_app="iTerm2"), cfg, state) is True


def test_idle_or_background_sends_when_idle():
    cfg = _config(gating="idle_or_background", idle_threshold_seconds=60)
    state = SystemState(idle_seconds=120, frontmost_app="iTerm2")
    assert should_send(_event(source_app="iTerm2"), cfg, state) is True


def test_idle_or_background_sends_when_backgrounded():
    cfg = _config(gating="idle_or_background", idle_threshold_seconds=60)
    state = SystemState(idle_seconds=2, frontmost_app="Safari")
    assert should_send(_event(source_app="iTerm2"), cfg, state) is True


def test_idle_or_background_suppresses_when_active_and_foreground():
    cfg = _config(gating="idle_or_background", idle_threshold_seconds=60)
    state = SystemState(idle_seconds=2, frontmost_app="iTerm2")
    assert should_send(_event(source_app="iTerm2"), cfg, state) is False


def test_idle_or_background_fails_open_when_both_unknown():
    cfg = _config(gating="idle_or_background")
    state = SystemState(idle_seconds=None, frontmost_app=None)
    assert should_send(_event(), cfg, state) is True


def test_idle_only_sends_when_idle():
    cfg = _config(gating="idle_only", idle_threshold_seconds=30)
    state = SystemState(idle_seconds=100, frontmost_app="iTerm2")
    assert should_send(_event(source_app="iTerm2"), cfg, state) is True


def test_idle_only_suppresses_when_active():
    cfg = _config(gating="idle_only", idle_threshold_seconds=30)
    state = SystemState(idle_seconds=5, frontmost_app="Safari")
    assert should_send(_event(source_app="iTerm2"), cfg, state) is False


def test_idle_only_fails_open_when_idle_unknown():
    cfg = _config(gating="idle_only")
    state = SystemState(idle_seconds=None, frontmost_app="iTerm2")
    assert should_send(_event(), cfg, state) is True


def test_background_only_sends_when_backgrounded():
    cfg = _config(gating="background_only")
    state = SystemState(idle_seconds=0, frontmost_app="Safari")
    assert should_send(_event(source_app="iTerm2"), cfg, state) is True


def test_background_only_suppresses_when_foreground():
    cfg = _config(gating="background_only")
    state = SystemState(idle_seconds=9999, frontmost_app="iTerm2")
    assert should_send(_event(source_app="iTerm2"), cfg, state) is False


def test_background_only_fails_open_when_frontmost_unknown():
    cfg = _config(gating="background_only")
    state = SystemState(idle_seconds=0, frontmost_app=None)
    assert should_send(_event(), cfg, state) is True


def test_unknown_source_app_treated_as_backgrounded():
    cfg = _config(gating="background_only")
    state = SystemState(idle_seconds=0, frontmost_app="iTerm2")
    assert should_send(_event(source_app=None), cfg, state) is True


def test_per_event_gating_overrides_global():
    cfg = _config(
        gating="idle_only",
        events={"permission": EventConfig(enabled=True, gating="always")},
    )
    state = SystemState(idle_seconds=0, frontmost_app="iTerm2")
    # permission is "always", so it sends
    assert should_send(_event("permission", "iTerm2"), cfg, state) is True
    # turn_complete still follows global "idle_only" and should suppress
    assert should_send(_event("turn_complete", "iTerm2"), cfg, state) is False
