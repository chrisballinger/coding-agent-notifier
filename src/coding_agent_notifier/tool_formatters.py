"""Tool-specific rendering for agent permission events.

A renderer takes a `tool_input` dict (shape varies per tool) and produces a
`ToolRender` with a one-line summary suitable for the message body and an
optional multi-line detail block for the code fence. Renderers also flag
dangerous operations so the sink can visually escalate them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from .event import truncate

DEFAULT_MAX_CHARS = 400

# Case-insensitive substrings that mark a Bash command as deserving a louder ping.
DANGEROUS_BASH_PATTERNS: tuple[str, ...] = (
    "rm -rf",
    "rm -fr",
    "sudo ",
    "sudo\t",
    "git push --force",
    "git push -f ",
    "git reset --hard",
    "dd if=",
    "chmod -r",
    "chown -r",
    "mkfs",
    ":(){:|:&};:",
    "> /dev/sda",
    "| sh",
    "| bash",
)


@dataclass(frozen=True)
class ToolRender:
    summary: str = ""
    detail: str | None = None
    dangerous: bool = False


Renderer = Callable[[dict[str, Any], int], ToolRender]


def render(tool_name: str | None, tool_input: dict[str, Any] | None, max_chars: int = DEFAULT_MAX_CHARS) -> ToolRender:
    """Dispatch to a tool-specific renderer, or fall back to generic JSON."""
    if not tool_input:
        return ToolRender()
    renderer = _REGISTRY.get(tool_name or "", _render_generic)
    return renderer(tool_input, max_chars)


# --- renderers ---


def _render_bash(tool_input: dict[str, Any], max_chars: int) -> ToolRender:
    command = str(tool_input.get("command") or "").strip()
    if not command:
        return ToolRender()
    return ToolRender(
        summary=_one_line(command, max_chars),
        detail=truncate(command, max_chars),
        dangerous=_is_dangerous_bash(command),
    )


def _render_exit_plan_mode(tool_input: dict[str, Any], max_chars: int) -> ToolRender:
    plan = str(tool_input.get("plan") or "").strip()
    if not plan:
        return ToolRender()
    title = _first_heading(plan) or _one_line(plan, 120)
    char_count = len(plan)
    summary = f"Plan: *{title}* _({char_count:,} chars)_"
    # Keep a short teaser in the detail (first ~max_chars of plan body, without
    # the leading heading) so reviewers see something actionable.
    body = plan
    if body.startswith("#"):
        # drop the first line (heading) from the detail teaser
        body = body.split("\n", 1)[1] if "\n" in body else ""
    detail = truncate(body.strip(), max_chars) if body.strip() else None
    return ToolRender(summary=summary, detail=detail)


def _render_file_edit(tool_input: dict[str, Any], max_chars: int) -> ToolRender:
    path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip()
    if not path:
        return ToolRender()
    # For Edit, try to hint at the change. For Write, skip the detail — file
    # content can be huge.
    new_str = tool_input.get("new_string")
    summary = f"`{path}`"
    detail = None
    if isinstance(new_str, str) and new_str.strip():
        detail = truncate(new_str.strip(), max_chars)
    return ToolRender(summary=summary, detail=detail)


def _render_write(tool_input: dict[str, Any], max_chars: int) -> ToolRender:
    path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip()
    if not path:
        return ToolRender()
    content = tool_input.get("content")
    size_hint = f" _({len(content):,} chars)_" if isinstance(content, str) else ""
    return ToolRender(summary=f"Write `{path}`{size_hint}")


def _render_multi_edit(tool_input: dict[str, Any], max_chars: int) -> ToolRender:
    path = str(tool_input.get("file_path") or "").strip()
    edits = tool_input.get("edits")
    n = len(edits) if isinstance(edits, list) else 0
    if not path and n == 0:
        return ToolRender()
    return ToolRender(summary=f"`{path}` — {n} edit{'s' if n != 1 else ''}")


def _render_read(tool_input: dict[str, Any], max_chars: int) -> ToolRender:
    path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip()
    if not path:
        return ToolRender()
    return ToolRender(summary=f"Read `{path}`")


def _render_task(tool_input: dict[str, Any], max_chars: int) -> ToolRender:
    agent = str(tool_input.get("subagent_type") or tool_input.get("description") or "agent")
    prompt = str(tool_input.get("prompt") or "").strip()
    first = _one_line(prompt, 160) if prompt else ""
    summary = f"Subagent *{agent}*"
    if first:
        summary += f": {first}"
    detail = truncate(prompt, max_chars) if prompt and len(prompt) > 160 else None
    return ToolRender(summary=summary, detail=detail)


def _render_generic(tool_input: dict[str, Any], max_chars: int) -> ToolRender:
    try:
        pretty = json.dumps(tool_input, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return ToolRender()
    return ToolRender(detail=truncate(pretty, max_chars))


_REGISTRY: dict[str, Renderer] = {
    "Bash": _render_bash,
    "ExitPlanMode": _render_exit_plan_mode,
    "Edit": _render_file_edit,
    "Write": _render_write,
    "MultiEdit": _render_multi_edit,
    "Read": _render_read,
    "Task": _render_task,
    # Codex names its shell tool `shell`
    "shell": _render_bash,
}


# --- helpers ---


def _is_dangerous_bash(command: str) -> bool:
    lowered = command.lower()
    return any(p in lowered for p in DANGEROUS_BASH_PATTERNS)


def _one_line(text: str, max_chars: int) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    return truncate(first, max_chars)


_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def _first_heading(markdown: str) -> str | None:
    m = _H1_RE.search(markdown)
    return m.group(1).strip() if m else None
