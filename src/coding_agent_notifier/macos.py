from __future__ import annotations

import subprocess
import sys


def is_macos() -> bool:
    return sys.platform == "darwin"


def idle_seconds() -> float | None:
    """Seconds since last HID event. Returns None on non-macOS or on failure."""
    if not is_macos():
        return None
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            # example: `    "HIDIdleTime" = 123456789`
            _, _, value = line.rpartition("=")
            try:
                return int(value.strip()) / 1_000_000_000
            except ValueError:
                return None
    return None


_APPLESCRIPT_FRONTMOST = (
    'tell application "System Events" to get name of first process whose frontmost is true'
)


def frontmost_app() -> str | None:
    """Human-readable name of the currently frontmost app (e.g. "iTerm2", "Terminal")."""
    if not is_macos():
        return None
    try:
        out = subprocess.run(
            ["osascript", "-e", _APPLESCRIPT_FRONTMOST],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    return out or None


# Map $TERM_PROGRAM values → AppleScript process names, so gating can compare them.
# Claude Code's Notification hook runs inside the terminal, so $TERM_PROGRAM identifies
# the parent app.
TERM_PROGRAM_TO_APP: dict[str, str] = {
    "iTerm.app": "iTerm2",
    "Apple_Terminal": "Terminal",
    "vscode": "Code",
    "WarpTerminal": "Warp",
    "ghostty": "Ghostty",
    "tmux": "",  # unknown parent; fall through
    "Hyper": "Hyper",
    "Alacritty": "Alacritty",
    "WezTerm": "WezTerm",
}


def term_program_to_app(term_program: str | None) -> str | None:
    if not term_program:
        return None
    return TERM_PROGRAM_TO_APP.get(term_program) or term_program
