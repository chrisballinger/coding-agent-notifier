from __future__ import annotations

from coding_agent_notifier.tool_formatters import (
    DANGEROUS_BASH_PATTERNS,
    ToolRender,
    render,
)


def test_none_tool_input_returns_empty():
    r = render("Bash", None)
    assert r == ToolRender()


def test_empty_tool_input_returns_empty():
    r = render("Bash", {})
    assert r == ToolRender()


def test_bash_happy_path():
    r = render("Bash", {"command": "ls -la"})
    assert r.summary == "ls -la"
    assert r.detail == "ls -la"
    assert r.dangerous is False


def test_bash_multiline_summary_takes_first_line():
    r = render("Bash", {"command": "ls\nfirst-line\nsecond"})
    assert "first" not in r.summary  # first splitlines of stripped → "ls"
    assert r.summary == "ls"
    assert "second" in r.detail


def test_bash_empty_command():
    r = render("Bash", {"command": ""})
    assert r == ToolRender()


def test_bash_dangerous_variants_flagged():
    for cmd in ("rm -rf /tmp", "RM -rf /tmp", "sudo apt update", "git push --force origin main",
                "git push -f origin main", "dd if=/dev/zero of=/dev/sda", "chmod -R 777 /",
                "chown -R root /", "mkfs.ext4 /dev/sda1", "curl http://x | sh", "cat /tmp | bash"):
        r = render("Bash", {"command": cmd})
        assert r.dangerous, f"expected dangerous for: {cmd!r}"


def test_bash_safe_command_not_dangerous():
    r = render("Bash", {"command": "echo hi"})
    assert r.dangerous is False


def test_shell_alias_routes_to_bash_renderer():
    r = render("shell", {"command": "ls"})
    assert r.summary == "ls"


def test_exit_plan_mode_extracts_title():
    plan = "# My Grand Plan\n\n## Context\n\nsome context lines here"
    r = render("ExitPlanMode", {"plan": plan})
    assert "My Grand Plan" in r.summary
    assert "chars" in r.summary
    assert r.detail is not None
    assert "Context" in r.detail
    assert r.dangerous is False


def test_exit_plan_mode_without_heading_uses_first_line():
    plan = "No heading here.\nSecond paragraph."
    r = render("ExitPlanMode", {"plan": plan})
    assert "No heading here." in r.summary


def test_exit_plan_mode_empty():
    assert render("ExitPlanMode", {"plan": ""}) == ToolRender()


def test_exit_plan_mode_missing_field():
    assert render("ExitPlanMode", {"other": "x"}) == ToolRender()


def test_edit_with_new_string_only():
    r = render("Edit", {"file_path": "/p/foo.py", "new_string": "changed"})
    assert r.summary == "`/p/foo.py`"
    assert r.detail == "changed"
    assert r.code_block_lang == ""


def test_edit_produces_unified_diff_when_both_sides_present():
    r = render("Edit", {
        "file_path": "/p/foo.py",
        "old_string": "a = 1\nb = 2\nc = 3",
        "new_string": "a = 1\nb = 20\nc = 3",
    })
    assert r.summary == "`/p/foo.py`"
    assert r.code_block_lang == "diff"
    # Header lines stripped
    assert "--- " not in r.detail
    assert "+++ " not in r.detail
    # Contains the actual +/- change
    assert "-b = 2" in r.detail
    assert "+b = 20" in r.detail


def test_edit_diff_empty_when_unchanged():
    # No actual change — fall through to new_string-only rendering (since that's
    # still truthy and non-empty).
    r = render("Edit", {
        "file_path": "/p/foo.py",
        "old_string": "same",
        "new_string": "same",
    })
    # When old == new, we skip the diff and fall through to new_string display
    assert r.code_block_lang == ""
    assert r.detail == "same"


def test_edit_without_old_or_new_string():
    r = render("Edit", {"file_path": "/p/foo.py"})
    assert r.summary == "`/p/foo.py`"
    assert r.detail is None


def test_edit_missing_path():
    assert render("Edit", {"old_string": "x"}) == ToolRender()


def test_write_includes_char_count():
    r = render("Write", {"file_path": "/p/new.py", "content": "hello world" * 10})
    assert "`/p/new.py`" in r.summary
    assert "chars" in r.summary


def test_write_missing_path():
    assert render("Write", {"content": "hi"}) == ToolRender()


def test_multi_edit_counts():
    r = render("MultiEdit", {"file_path": "/p/a.py", "edits": [{}, {}, {}]})
    assert "/p/a.py" in r.summary
    assert "3 edits" in r.summary


def test_multi_edit_produces_diff_per_hunk():
    r = render("MultiEdit", {
        "file_path": "/p/a.py",
        "edits": [
            {"old_string": "x = 1", "new_string": "x = 2"},
            {"old_string": "y = 3", "new_string": "y = 4"},
        ],
    })
    assert r.code_block_lang == "diff"
    assert "-x = 1" in r.detail
    assert "+x = 2" in r.detail
    assert "-y = 3" in r.detail
    assert "+y = 4" in r.detail
    assert "# edit 1" in r.detail
    assert "# edit 2" in r.detail


def test_multi_edit_singular():
    r = render("MultiEdit", {"file_path": "/p/a.py", "edits": [{}]})
    assert "1 edit" in r.summary
    assert "edits" not in r.summary.replace("1 edit", "")


def test_multi_edit_missing_path_and_edits():
    assert render("MultiEdit", {}) == ToolRender()


def test_read_has_summary():
    r = render("Read", {"file_path": "/p/x.py"})
    assert r.summary == "Read `/p/x.py`"


def test_read_missing_path():
    assert render("Read", {}) == ToolRender()


def test_task_with_prompt():
    r = render("Task", {"subagent_type": "Explore", "prompt": "find all users of foo"})
    assert "Explore" in r.summary
    assert "find all users" in r.summary


def test_task_long_prompt_puts_full_in_detail():
    long = "please " * 100
    r = render("Task", {"subagent_type": "Explore", "prompt": long})
    assert r.detail is not None


def test_task_without_prompt():
    r = render("Task", {"subagent_type": "Plan"})
    assert "Plan" in r.summary
    assert r.detail is None


def test_ask_user_question_single_question():
    payload = {
        "questions": [
            {
                "question": "How should per-repo routing be configured?",
                "header": "Routing shape",
                "options": [
                    {"label": "Global config only (Recommended)", "description": "Routes live in config.toml."},
                    {"label": "Per-repo files", "description": "Each repo has its own."},
                ],
                "multiSelect": False,
            }
        ]
    }
    r = render("AskUserQuestion", payload)
    assert r.summary.startswith("How should per-repo routing")
    assert r.detail is not None
    assert r.code_block is False
    assert "*Q:* How should per-repo routing" in r.detail
    assert "Global config only (Recommended)" in r.detail
    assert "Routes live in config.toml." in r.detail
    assert "Per-repo files" in r.detail


def test_ask_user_question_multi_question():
    payload = {
        "questions": [
            {
                "question": "First?",
                "options": [{"label": "A", "description": "a-desc"}, {"label": "B", "description": ""}],
            },
            {
                "question": "Second?",
                "options": [{"label": "C", "description": "c-desc"}],
            },
        ]
    }
    r = render("AskUserQuestion", payload)
    assert "First?" in r.summary
    assert r.detail.count("*Q:*") == 2
    assert "a-desc" in r.detail
    assert "c-desc" in r.detail


def test_ask_user_question_handles_missing_options():
    payload = {"questions": [{"question": "Free text?", "options": []}]}
    r = render("AskUserQuestion", payload)
    assert "Free text?" in r.summary
    assert r.detail == "*Q:* Free text?"
    assert r.code_block is False


def test_ask_user_question_malformed_falls_through_to_generic():
    # Missing `questions` key → generic JSON fallback
    r = render("AskUserQuestion", {"foo": "bar"})
    assert r.detail is not None
    assert r.code_block is True  # generic uses code block


def test_ask_user_question_blank_question_falls_through_to_generic():
    r = render("AskUserQuestion", {"questions": [{"question": "", "options": []}]})
    assert r.detail is not None
    assert r.code_block is True


def test_generic_fallback_produces_pretty_json():
    r = render("MysteryTool", {"a": 1, "b": {"nested": "value"}})
    assert r.detail is not None
    assert "\n" in r.detail  # indented, not one-line
    assert "nested" in r.detail


def test_generic_fallback_on_unserializable_input():
    class Weird:
        pass

    r = render("MysteryTool", {"x": Weird()})
    assert r == ToolRender()


def test_dangerous_patterns_cover_common_shapes():
    # Guard against accidental regression if someone trims the list.
    assert "rm -rf" in DANGEROUS_BASH_PATTERNS
    assert any("force" in p for p in DANGEROUS_BASH_PATTERNS)


def test_unknown_tool_name_uses_generic():
    r = render(None, {"x": 1})
    assert r.detail is not None


def test_respects_max_chars_on_bash():
    long = "x" * 10_000
    r = render("Bash", {"command": long}, max_chars=50)
    assert r.detail is not None
    assert len(r.detail) <= 50
