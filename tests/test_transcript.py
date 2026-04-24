from __future__ import annotations

import json
from pathlib import Path

from coding_agent_notifier.transcript import (
    head_tail_snippet,
    read_last_assistant_text,
)


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(obj) for obj in lines))


def test_last_assistant_text_nested_shape(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "First reply."},
                        {"type": "tool_use", "name": "Bash"},
                    ],
                },
            },
            {"type": "user", "message": {"role": "user", "content": "and now?"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Second reply."}],
                },
            },
        ],
    )
    assert read_last_assistant_text(transcript) == "Second reply."


def test_last_assistant_text_flat_shape(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "Hello!"}]},
        ],
    )
    assert read_last_assistant_text(transcript) == "Hello!"


def test_last_assistant_text_string_content(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [{"role": "assistant", "content": "just a string"}],
    )
    assert read_last_assistant_text(transcript) == "just a string"


def test_last_assistant_text_skips_trailing_tool_use_only_turn(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Here's the plan"}],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                },
            },
        ],
    )
    assert read_last_assistant_text(transcript) == "Here's the plan"


def test_last_assistant_text_concatenates_text_blocks(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Block one."},
                        {"type": "text", "text": "Block two."},
                    ],
                },
            },
        ],
    )
    text = read_last_assistant_text(transcript)
    assert text is not None
    assert "Block one." in text
    assert "Block two." in text


def test_malformed_lines_skipped(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "not json\n"
        + json.dumps({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        + "\n"
    )
    assert read_last_assistant_text(transcript) == "ok"


def test_missing_file_returns_none(tmp_path: Path):
    assert read_last_assistant_text(tmp_path / "nope.jsonl") is None


def test_empty_file_returns_none(tmp_path: Path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert read_last_assistant_text(p) is None


def test_no_assistant_lines_returns_none(tmp_path: Path):
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, [{"role": "user", "content": "hi"}])
    assert read_last_assistant_text(p) is None


def test_head_tail_passthrough_short():
    assert head_tail_snippet("hello world") == "hello world"


def test_head_tail_snippet_long_text():
    text = "A" * 400 + "B" * 400
    snippet = head_tail_snippet(text, head=100, tail=100)
    assert snippet.startswith("A" * 100)
    assert snippet.endswith("B" * 100)
    assert "…" in snippet


def test_head_tail_strips_whitespace():
    assert head_tail_snippet("   short   ") == "short"


def test_head_tail_zero_both_empty():
    assert head_tail_snippet("anything", head=0, tail=0) == ""


def test_head_tail_only_head_appends_ellipsis():
    text = "x" * 1000
    snippet = head_tail_snippet(text, head=50, tail=0)
    assert snippet == "x" * 50 + "…"


def test_head_tail_snaps_head_to_word_boundary():
    # Sentence designed so char 250 lands in the middle of "sa_within".
    prefix = "A" * 240 + " the sawithin"  # char 250 lands in "sawithin"
    suffix = " B" * 300
    text = prefix + suffix
    snippet = head_tail_snippet(text, head=250, tail=100)
    # Must not end with a truncated word like "sa"
    head = snippet.split("\n…\n")[0]
    assert not head.endswith("sa"), f"head ended mid-word: {head!r}"
    # Must end at the last whitespace boundary within the window
    assert head.endswith("the")


def test_head_tail_snaps_tail_to_word_boundary():
    prefix = "A " * 300
    # Deliberately arrange so text[-250:] begins mid-word ("ithin").
    suffix_tail = "within the tail " + "B" * 240
    text = prefix + suffix_tail
    snippet = head_tail_snippet(text, head=100, tail=250)
    tail = snippet.split("\n…\n")[1]
    # Tail must start at a word boundary — not mid-word like "ithin".
    assert not tail.startswith("ithin")
    # First "word" in tail should appear intact in the original text
    first_word = tail.split(" ", 1)[0].split("\n", 1)[0]
    assert f" {first_word}" in text or text.startswith(first_word)


def test_head_tail_no_whitespace_preserves_content():
    """A giant URL or base64 with no spaces shouldn't collapse to empty after snapping."""
    text = "x" * 800
    snippet = head_tail_snippet(text, head=100, tail=100)
    # Should still have substantive content on both sides of the ellipsis
    head, _, tail = snippet.partition("\n…\n")
    assert len(head) > 50
    assert len(tail) > 50


def test_head_tail_only_tail_prepends_ellipsis():
    text = "y" * 1000
    snippet = head_tail_snippet(text, head=0, tail=50)
    assert snippet == "…" + "y" * 50
