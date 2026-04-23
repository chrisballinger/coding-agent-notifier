from __future__ import annotations

from pathlib import Path

from coding_agent_notifier.event import Event, truncate


def test_event_title_and_emoji():
    ev = Event(agent="claude-code", kind="permission", message="m", cwd=Path("/x"))
    assert ev.title.startswith("Claude Code")
    assert ev.emoji.startswith(":")


def test_event_titles_cover_all_kinds():
    kinds = ("permission", "idle_prompt", "turn_complete", "elicitation")
    for kind in kinds:
        ev = Event(agent="codex", kind=kind, message="m", cwd=Path("/x"))
        assert ev.title.startswith("Codex")
        assert ev.emoji


def test_truncate_short_text_unchanged():
    assert truncate("hello") == "hello"


def test_truncate_trims_long_text():
    out = truncate("a" * 500, limit=20)
    assert out.endswith("…")
    assert len(out) == 20


def test_truncate_strips_whitespace():
    assert truncate("   hi   ") == "hi"
