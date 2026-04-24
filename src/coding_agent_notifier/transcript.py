"""Read the last assistant turn from a Claude Code transcript JSONL.

Each line in a transcript is one event: a user message, an assistant message
(which may contain multiple content blocks — text, tool_use, etc.), or a
tool_result. We care about the *most recent* assistant message that contains
plain text, so we scan backward and concatenate `type == "text"` blocks.

Two shapes appear in the wild, both handled:
- nested:   {"type":"assistant","message":{"role":"assistant","content":[...]}}
- flat:     {"role":"assistant","content":[...]}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_last_assistant_text(path: Path) -> str | None:
    """Return the text of the most recent assistant turn, or None on any failure.

    Failures (missing file, unreadable, malformed JSON) return None rather than
    raising — the caller is a notification pipeline, not a data integrity tool.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in reversed(raw.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        text = _extract_assistant_text(obj)
        if text:
            return text
    return None


def head_tail_snippet(text: str, *, head: int = 250, tail: int = 250) -> str:
    """Return `text` unchanged if short; else head+ellipsis+tail preview.

    Snaps each cut to the nearest whitespace so words don't break mid-letter.
    With head=0 only the tail is kept and `…` is prefixed; with tail=0 only the
    head is kept and `…` is appended. With both 0 the result is empty.
    """
    text = text.strip()
    if head <= 0 and tail <= 0:
        return ""
    if len(text) <= head + tail + 5:
        return text
    head_part = _snap_head(text[:head]) if head > 0 else ""
    tail_part = _snap_tail(text[-tail:]) if tail > 0 else ""
    if head_part and tail_part:
        return f"{head_part}\n…\n{tail_part}"
    if head_part:
        return f"{head_part}…"
    return f"…{tail_part}"


def _snap_head(chunk: str) -> str:
    """Trim `chunk` back to the last whitespace so we never end mid-word.

    Only snaps when at least half the budget is preserved — otherwise a word
    with no internal whitespace (e.g. a URL) would collapse to an empty string.
    """
    stripped = chunk.rstrip()
    idx = max(stripped.rfind(" "), stripped.rfind("\n"))
    if idx > len(stripped) // 2:
        stripped = stripped[:idx].rstrip()
    return stripped


def _snap_tail(chunk: str) -> str:
    """Trim `chunk` forward from the first whitespace so we never start mid-word."""
    stripped = chunk.lstrip()
    idx_space = stripped.find(" ")
    idx_nl = stripped.find("\n")
    cuts = [i for i in (idx_space, idx_nl) if i >= 0]
    if not cuts:
        return stripped
    idx = min(cuts)
    if 0 < idx < len(stripped) // 2:
        stripped = stripped[idx + 1:].lstrip()
    return stripped


def _extract_assistant_text(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    # Shape 1 (nested): top-level has "type" discriminator; real message under "message".
    # Shape 2 (flat):   obj itself has "role" and "content".
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    if not isinstance(msg, dict):
        return ""
    role = msg.get("role")
    if role != "assistant" and obj.get("type") != "assistant":
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n\n".join(parts)
    return ""
