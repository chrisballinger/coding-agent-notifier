"""Convert standard markdown to Slack's `mrkdwn` dialect.

Slack's `section`/`mrkdwn` text is intentionally non-CommonMark:

  - bold:    `*x*` (single asterisk; double `**` doesn't work)
  - italic:  `_x_` (single underscore; `*` always renders as bold)
  - strike:  `~x~` (single tilde)
  - link:    `<url|label>` (angle-bracketed pipe form)
  - heading: no syntax — convention is bold on its own line
  - list:    no syntax — convention is `• item` (Unicode bullet)
  - code:    `` `inline` `` and ` ```fenced``` ` — match standard markdown
  - quote:   `> quoted` — matches standard markdown

`to_slack_mrkdwn(text)` is a single-pass-ish pipeline that walks the rules
in load-bearing order:

  1. Extract code regions (fenced + inline) into placeholder tokens so the
     other rules can't touch markdown that appears INSIDE code.
  2. Markdown tables → bullet-list per row (`- *col:* val`). Slack mrkdwn
     has no table syntax; bullet-per-row keeps cell markdown live so each
     cell still flows through bold/italic/link conversion below.
  3. Markdown links `[label](url)` → `<url|label>`. Must come BEFORE bare-
     URL handling so the trailing `)` doesn't get eaten by a greedy URL
     regex.
  4. Bare URLs → `<url|host>`. Stops at unbalanced closing parens to avoid
     dragging the `)` from a markdown link's tail.
  5. Bold `**x**` / `__x__` → `*x*`. Before italic so the bold pair isn't
     half-converted.
  6. Italic `*x*` / `_x_` → `_x_`.
  7. Strikethrough `~~x~~` → `~x~`.
  8. Headings ``^#{1,6}\\s+(.+)$`` → `*<title>*`. All levels collapse to
     bold (Slack's own `markdown` block does the same).
  9. Bullets `^[-*+]\s+` → `• `. Numbered lists left as-is.
 10. Path-as-inline-code (`_PATH_RE`) — keep an existing readability win.
 11. Restore code-region placeholders byte-for-byte.

Discord is NOT in scope — it accepts close-to-CommonMark natively and
running this converter on its output would actively break it (`**bold**`
→ `*bold*` reads as italic in Discord). Slack only.
"""
from __future__ import annotations

import re

# Placeholder tokens use NUL chars so they can't collide with normal agent
# text. Indexes are encoded in decimal between two NULs. Two namespaces:
# `C` for protected code regions (full text restored as-is) and `B` for
# bold spans (the inner italic pass shouldn't double-convert them; restored
# as `*<text>*` after italic runs).
_CODE_PLACEHOLDER_RE = re.compile(r"\x00C(\d+)\x00")
_BOLD_PLACEHOLDER_RE = re.compile(r"\x00B(\d+)\x00")

# Fenced code blocks. Multiline; non-greedy so we don't merge two fences.
_FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
# Inline code. Greedy single-line match between backticks; doesn't cross
# newlines (avoids swallowing a whole paragraph if a stray backtick exists).
_INLINE_CODE_RE = re.compile(r"`[^`\n]+?`")

# Markdown link: [label](url). The URL alternation matches either runs of
# non-paren non-space chars OR a balanced `(...)` group, so URLs containing
# parens (Wikipedia-style) capture correctly without dragging in the link's
# closing `)`. Single nesting level — adequate for real URLs.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(((?:[^()\s]+|\([^)]*\))+)\)")

# Bare URLs that aren't already inside a Slack link. The exclusion avoids
# matching the URL portion of a `<url|label>` we just emitted (NUL is also
# excluded so we don't eat into placeholder tokens).
_BARE_URL_RE = re.compile(r"(?<![<`|\x00])\bhttps?://[^\s<>`)\]\x00]+")

# Bold: **x** or __x__. Non-greedy across the inner text; allows inner `*`
# (so `**bold *and* italic**` matches with `bold *and* italic` as the inner
# span). Disallows newlines so a half-open `**` doesn't gobble forever.
_BOLD_RE = re.compile(r"(?<!\\)\*\*([^\n]+?)\*\*")
_BOLD_UNDERSCORE_RE = re.compile(r"(?<!\\)__([^\n]+?)__")

# Italic: single * or single _. Must run AFTER bold. Word boundaries help
# avoid converting `path/with*splat` inside identifiers.
_ITALIC_STAR_RE = re.compile(r"(?<![\\\w*])\*(?!\s)([^\n*]+?)(?<!\s)\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\\\w_])_(?!\s)([^\n_]+?)(?<!\s)_(?![\w_])")

# Strike: ~~x~~
_STRIKE_RE = re.compile(r"~~([^\n~]+?)~~")

# Heading: leading # # # then space then title.
_HEADING_RE = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*$")

# Unordered-list bullet at line start: -, *, or + followed by whitespace.
# Indentation preserved so nested lists keep their shape.
_BULLET_RE = re.compile(r"(?m)^([ \t]*)[-*+][ \t]+")

# Path → inline code (preserved from the original `_mrkdwn_polish`).
# Matches `/...` not preceded by `:`/letter/digit/backtick (so URLs and
# identifier fragments don't trigger).
_PATH_RE = re.compile(r"(?<![`:/A-Za-z0-9])(/[^\s`<>]+)")


def to_slack_mrkdwn(text: str) -> str:
    """Convert standard-markdown `text` into Slack-flavoured mrkdwn."""
    if not text:
        return text

    text, code_regions = _extract_code(text)

    text = _convert_tables(text)

    text = _MD_LINK_RE.sub(_format_md_link, text)
    text = _BARE_URL_RE.sub(_format_bare_url, text)

    # Convert bold first, but stash the output in a placeholder so the
    # italic pass doesn't re-match the now-single-asterisk pair as italic.
    bold_spans: list[str] = []

    def _stash_bold(match: re.Match[str]) -> str:
        # Recursively convert italic INSIDE the bold span — `**a *b* c**`
        # should produce `*a _b_ c*`. The italic regexes are safe to run
        # against an isolated span.
        inner = match.group(1)
        inner = _ITALIC_STAR_RE.sub(r"_\1_", inner)
        inner = _ITALIC_UNDER_RE.sub(r"_\1_", inner)
        bold_spans.append(inner)
        return f"\x00B{len(bold_spans) - 1}\x00"

    text = _BOLD_RE.sub(_stash_bold, text)
    text = _BOLD_UNDERSCORE_RE.sub(_stash_bold, text)

    text = _ITALIC_STAR_RE.sub(r"_\1_", text)
    text = _ITALIC_UNDER_RE.sub(r"_\1_", text)

    text = _STRIKE_RE.sub(r"~\1~", text)

    # Restore bold placeholders to mrkdwn `*x*` form. Done before headings/
    # bullets so a heading containing bold renders correctly.
    text = _BOLD_PLACEHOLDER_RE.sub(
        lambda m: f"*{bold_spans[int(m.group(1))]}*"
        if 0 <= int(m.group(1)) < len(bold_spans) else m.group(0),
        text,
    )

    text = _HEADING_RE.sub(r"*\1*", text)

    text = _BULLET_RE.sub(r"\1• ", text)

    text = _PATH_RE.sub(lambda m: f"`{m.group(1).rstrip('.,)')}`", text)

    text = _restore_code(text, code_regions)
    return text


# Recognises a markdown table separator cell: optional `:` for alignment,
# one or more `-`, optional trailing `:`. Whitespace tolerated either side.
_SEP_CELL_RE = re.compile(r"^\s*:?-+:?\s*$")


def _convert_tables(text: str) -> str:
    """Convert markdown tables to bullet-list-per-row.

    A table is a header row + separator row (---) + one-or-more data rows,
    each line bracketed with outer `|` chars. Output: one bullet block per
    data row, with header cells as bold prefixes (`- *col:* val`) when the
    table has more than one column. Single-column tables emit `- val`.

    Runs after `_extract_code` so tables INSIDE fenced code regions stay
    literal (the whole fence is already a placeholder by this point).
    """
    if "|" not in text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if (
            i + 1 < len(lines)
            and _looks_like_table_row(lines[i])
            and _is_separator_row(lines[i + 1])
        ):
            headers = _split_table_row(lines[i])
            j = i + 2
            rows: list[list[str]] = []
            while j < len(lines) and _looks_like_table_row(lines[j]):
                rows.append(_split_table_row(lines[j]))
                j += 1
            if rows:
                out.append(_render_table_as_bullets(headers, rows))
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _looks_like_table_row(line: str) -> bool:
    s = line.strip()
    # Need at least the two outer pipes plus content between them. A bare
    # `|` or `||` doesn't qualify. False positives are caught downstream by
    # the separator-row requirement.
    return len(s) >= 3 and s.startswith("|") and s.endswith("|")


def _is_separator_row(line: str) -> bool:
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return False
    cells = s.strip("|").split("|")
    return bool(cells) and all(_SEP_CELL_RE.match(c) for c in cells)


def _split_table_row(line: str) -> list[str]:
    s = line.strip().strip("|")
    return [c.strip() for c in s.split("|")]


def _render_table_as_bullets(headers: list[str], rows: list[list[str]]) -> str:
    named = len(headers) > 1 and any(h for h in headers)
    blocks: list[str] = []
    for row in rows:
        if named:
            cols = max(len(headers), len(row))
            padded_row = list(row) + [""] * (cols - len(row))
            padded_headers = list(headers) + [""] * (cols - len(headers))
            cell_lines: list[str] = []
            for idx, (h, v) in enumerate(zip(padded_headers, padded_row)):
                prefix = "- " if idx == 0 else "  "
                if h:
                    cell_lines.append(f"{prefix}**{h}:** {v}".rstrip())
                else:
                    cell_lines.append(f"{prefix}{v}".rstrip())
            blocks.append("\n".join(cell_lines))
        else:
            value = " ".join(c for c in row if c)
            blocks.append(f"- {value}")
    return "\n\n".join(blocks)


def _extract_code(text: str) -> tuple[str, list[str]]:
    """Replace fenced + inline code spans with `\x00C<n>\x00` placeholders."""
    regions: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        regions.append(match.group(0))
        return f"\x00C{len(regions) - 1}\x00"

    text = _FENCE_RE.sub(_stash, text)
    text = _INLINE_CODE_RE.sub(_stash, text)
    return text, regions


def _restore_code(text: str, regions: list[str]) -> str:
    def _put_back(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return regions[idx] if 0 <= idx < len(regions) else match.group(0)

    return _CODE_PLACEHOLDER_RE.sub(_put_back, text)


def _format_md_link(match: re.Match[str]) -> str:
    label = match.group(1).strip()
    url = match.group(2).rstrip(".,")
    return f"<{url}|{label}>"


def _format_bare_url(match: re.Match[str]) -> str:
    url = match.group(0).rstrip(".,")
    host = re.sub(r"^https?://", "", url).split("/", 1)[0]
    return f"<{url}|{host}>"
