from __future__ import annotations

import pytest

from coding_agent_notifier.sinks.mrkdwn import to_slack_mrkdwn


@pytest.mark.parametrize(
    "src, expected",
    [
        # Bold ----------------------------------------------------------
        ("**bold**", "*bold*"),
        ("plain **bold** plain", "plain *bold* plain"),
        ("__under-bold__", "*under-bold*"),
        # Two bolds on one line.
        ("**a** and **b**", "*a* and *b*"),
        # Italic --------------------------------------------------------
        ("*italic*", "_italic_"),
        ("_italic_", "_italic_"),
        # Italic inside bold (bold runs first; remaining * italics get
        # converted in the second pass).
        ("**bold *and* italic**", "*bold _and_ italic*"),
        # Bare * with no closing: leave alone.
        ("a * b * c", "a * b * c"),
        # Strike --------------------------------------------------------
        ("~~strike~~", "~strike~"),
        # Headings ------------------------------------------------------
        ("# H1", "*H1*"),
        ("## H2", "*H2*"),
        ("### H3", "*H3*"),
        ("###### H6", "*H6*"),
        # Heading mid-paragraph: only matches at line start.
        ("not a # heading", "not a # heading"),
        # Bullets -------------------------------------------------------
        ("- one\n- two\n- three", "• one\n• two\n• three"),
        ("* one\n* two", "• one\n• two"),
        ("+ one", "• one"),
        # Indented bullets keep their indentation.
        ("- top\n  - nested", "• top\n  • nested"),
        # Numbered lists left as-is (Slack renders them readable).
        ("1. one\n2. two", "1. one\n2. two"),
        # Markdown links -----------------------------------------------
        ("[Slack](https://slack.com)", "<https://slack.com|Slack>"),
        ("see [docs](https://example.com/path) here", "see <https://example.com/path|docs> here"),
        # Bare URLs ----------------------------------------------------
        ("https://example.com/x", "<https://example.com/x|example.com>"),
        # Combined: link + trailing punctuation.
        ("see [docs](https://example.com).", "see <https://example.com|docs>."),
        # Empty / whitespace -------------------------------------------
        ("", ""),
        ("   ", "   "),
        # Quote untouched ----------------------------------------------
        ("> quoted line", "> quoted line"),
    ],
)
def test_converts(src: str, expected: str):
    assert to_slack_mrkdwn(src) == expected


def test_inline_code_preserved():
    """Markdown syntax INSIDE inline code stays literal."""
    src = "the `**stars**` and `## hashes` stay literal"
    out = to_slack_mrkdwn(src)
    assert "`**stars**`" in out
    assert "`## hashes`" in out


def test_fenced_code_block_preserved():
    """Markdown syntax inside a fenced code block stays literal — no
    accidental bold/heading conversion."""
    src = "before\n\n```python\n# comment **not bold**\n```\n\nafter"
    out = to_slack_mrkdwn(src)
    assert "```python\n# comment **not bold**\n```" in out
    # 'before'/'after' are unchanged plain text.
    assert "before" in out and "after" in out


def test_bold_inside_code_not_converted():
    """Concrete regression: don't convert `**` that lives inside backticks."""
    src = "`a**b**c` and **outside**"
    out = to_slack_mrkdwn(src)
    assert "`a**b**c`" in out  # untouched
    assert "*outside*" in out  # converted


def test_balanced_parens_in_url():
    """URLs containing parens (Wikipedia-style) should not be split."""
    src = "[wiki](https://en.wikipedia.org/wiki/Foo_(bar))"
    out = to_slack_mrkdwn(src)
    assert out == "<https://en.wikipedia.org/wiki/Foo_(bar)|wiki>"


def test_path_inline_code_still_works():
    """The path → inline-code rule from the original _mrkdwn_polish is
    preserved by the new converter."""
    src = "edit /Users/me/file.py please"
    assert "`/Users/me/file.py`" in to_slack_mrkdwn(src)


def test_realistic_agent_message():
    """End-to-end smoke: a realistic agent end-of-turn summary should
    render bold, headings, links, and bullets correctly."""
    src = """## Summary

Made the **following** changes:

- Updated *one* file
- Added a [reference](https://example.com/docs) to docs
- Removed ~~deprecated~~ helper

End of summary."""
    out = to_slack_mrkdwn(src)
    assert "*Summary*" in out
    assert "*following*" in out
    assert "_one_" in out
    assert "<https://example.com/docs|reference>" in out
    assert "~deprecated~" in out
    assert "• Updated" in out
    assert "## Summary" not in out
    assert "**following**" not in out


def test_half_open_bold_left_literal():
    """A `**` with no matching close shouldn't gobble the rest of the
    text. We accept it staying literal."""
    src = "this is **half-open and continues forever"
    out = to_slack_mrkdwn(src)
    assert "**half-open" in out  # not eaten


# --- Tables ---


def test_two_column_table_renders_as_bullet_blocks():
    """The header cells become bold prefixes on each row's bullet block."""
    src = (
        "| Issue | Fix |\n"
        "| --- | --- |\n"
        "| Plan in code fence | render returns code_block=False |\n"
        "| tool.detail not converted | run polish on non-fence detail |\n"
    )
    out = to_slack_mrkdwn(src)
    # Separator row must be gone.
    assert "---" not in out
    assert "|" not in out
    # Each row becomes a bullet block.
    assert "• *Issue:* Plan in code fence" in out
    assert "*Fix:* render returns code_block=False" in out
    assert "• *Issue:* tool.detail not converted" in out
    assert "*Fix:* run polish on non-fence detail" in out


def test_single_column_table_drops_header_label():
    src = (
        "| Items |\n"
        "| --- |\n"
        "| first |\n"
        "| second |\n"
    )
    out = to_slack_mrkdwn(src)
    assert "• first" in out
    assert "• second" in out
    assert "Items" not in out  # no header label for 1-col


def test_table_with_markdown_in_cells_still_converts_cells():
    """Cell content flows through the rest of the pipeline — bold/link/code
    inside cells render correctly."""
    src = (
        "| Name | Link |\n"
        "| --- | --- |\n"
        "| **bolded** | [docs](https://example.com) |\n"
    )
    out = to_slack_mrkdwn(src)
    assert "*Name:* *bolded*" in out
    assert "*Link:* <https://example.com|docs>" in out


def test_table_with_alignment_separators_recognised():
    """`:---` / `---:` / `:---:` alignment markers are still separators."""
    src = (
        "| L | C | R |\n"
        "| :--- | :---: | ---: |\n"
        "| 1 | 2 | 3 |\n"
    )
    out = to_slack_mrkdwn(src)
    assert "• *L:* 1" in out
    assert "*C:* 2" in out
    assert "*R:* 3" in out


def test_table_inside_fence_stays_literal():
    """A markdown table inside a fenced code block must not be converted."""
    src = (
        "before\n\n"
        "```\n"
        "| col | val |\n"
        "| --- | --- |\n"
        "| a | b |\n"
        "```\n\n"
        "after"
    )
    out = to_slack_mrkdwn(src)
    assert "| col | val |" in out
    assert "| --- | --- |" in out
    assert "| a | b |" in out


def test_lone_pipe_line_not_treated_as_table():
    """A header-looking line with no separator row is left alone."""
    src = "| this | is | not | a | table |\n\nFollowed by prose."
    out = to_slack_mrkdwn(src)
    assert "| this | is | not | a | table |" in out


def test_empty_table_with_no_rows_left_alone():
    """Header + separator with zero data rows is not a useful table —
    leave the lines as-is."""
    src = "| col1 | col2 |\n| --- | --- |\n"
    out = to_slack_mrkdwn(src)
    assert "| col1 | col2 |" in out


def test_table_followed_by_prose():
    """Conversion stops at the first non-table line."""
    src = (
        "| K | V |\n"
        "| --- | --- |\n"
        "| a | 1 |\n"
        "\n"
        "Some prose after."
    )
    out = to_slack_mrkdwn(src)
    assert "• *K:* a" in out
    assert "*V:* 1" in out
    assert "Some prose after." in out


def test_no_pipes_short_circuits():
    """If there are no `|` chars at all, the table pass is a no-op."""
    src = "no tables here\njust prose"
    assert to_slack_mrkdwn(src) == "no tables here\njust prose"
