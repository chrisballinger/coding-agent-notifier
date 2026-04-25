from __future__ import annotations

from pathlib import Path

from coding_agent_notifier.event import Event, chunk_text, truncate


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


def test_chunk_text_empty_returns_empty_list():
    assert chunk_text("", chunk_size=100) == []
    assert chunk_text("   \n\t  ", chunk_size=100) == []


def test_chunk_text_short_fits_in_one_chunk():
    out = chunk_text("hello world", chunk_size=100)
    assert out == ["hello world"]


def test_chunk_text_splits_at_chunk_size_boundary():
    text = "a" * 250
    out = chunk_text(text, chunk_size=100)
    assert out == ["a" * 100, "a" * 100, "a" * 50]


def test_chunk_text_max_chars_caps_total_with_ellipsis():
    text = "a" * 500
    out = chunk_text(text, chunk_size=100, max_chars=50)
    assert out == ["a" * 49 + "…"]


def test_chunk_text_max_chars_zero_means_full():
    text = "a" * 250
    out = chunk_text(text, chunk_size=100, max_chars=0)
    assert "".join(out) == text  # no truncation, no ellipsis
    assert not out[-1].endswith("…")


def test_chunk_text_max_chars_larger_than_text_no_truncation():
    text = "a" * 50
    out = chunk_text(text, chunk_size=100, max_chars=1000)
    assert out == ["a" * 50]
