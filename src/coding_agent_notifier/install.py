from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import tomlkit

from . import paths

CLAUDE_HOOK_COMMAND = "agent-notify hook --source claude-code"
CODEX_HOOK_COMMAND_PARTS = ["agent-notify", "hook", "--source", "codex"]

# PermissionRequest is a blocking decision hook — its timeout must be long
# enough to survive a lock-screen approval round-trip. 10min matches Claude
# Code's documented default; we make it explicit so a user with a shorter
# global default doesn't fail-close prematurely.
PERMISSIONREQUEST_TIMEOUT_SECONDS = 600

CLAUDE_PERMISSIONREQUEST_ENTRIES: dict[str, list[dict[str, Any]]] = {
    "PermissionRequest": [
        {
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": CLAUDE_HOOK_COMMAND,
                    "timeout": PERMISSIONREQUEST_TIMEOUT_SECONDS,
                }
            ],
        }
    ],
}

# PostToolUse is the back-fill signal that lets us update a Slack approval
# message after the user answered on a non-Slack surface (TUI, Claude Code
# Remote on iOS). The matcher is narrow — only the two tools we collapse
# Slack messages for — so we don't pay a hook subprocess per tool call.
# Installed alongside PermissionRequest by `install_slack_bot`; not part of
# the base notification install.
CLAUDE_POSTTOOLUSE_ENTRIES: dict[str, list[dict[str, Any]]] = {
    "PostToolUse": [
        {
            "matcher": "AskUserQuestion|ExitPlanMode",
            "hooks": [{"type": "command", "command": CLAUDE_HOOK_COMMAND}],
        }
    ],
}

# Base notification hooks. PermissionRequest is intentionally NOT here — it
# becomes a blocking decision hook only when the Slack bot install runs (see
# CLAUDE_PERMISSIONREQUEST_ENTRIES). For non-actionable installs the
# Notification hook with matcher "permission_prompt" already pings the user
# for permission events.
CLAUDE_HOOK_ENTRIES: dict[str, list[dict[str, Any]]] = {
    "Notification": [
        {
            "matcher": "permission_prompt|idle_prompt|elicitation_dialog",
            "hooks": [{"type": "command", "command": CLAUDE_HOOK_COMMAND}],
        }
    ],
    "Stop": [
        {
            "matcher": "*",
            "hooks": [{"type": "command", "command": CLAUDE_HOOK_COMMAND}],
        }
    ],
    # UserPromptSubmit is a control signal, not a ping source — we use it to
    # reset the per-session cross-kind coalesce marker so the next turn's
    # turn_complete / idle_prompt can ping cleanly.
    "UserPromptSubmit": [
        {
            "matcher": "*",
            "hooks": [{"type": "command", "command": CLAUDE_HOOK_COMMAND}],
        }
    ],
}

CODEX_HOOK_ENTRIES: dict[str, list[dict[str, Any]]] = {
    "Stop": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": " ".join(CODEX_HOOK_COMMAND_PARTS),
                }
            ]
        }
    ],
    "PermissionRequest": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": " ".join(CODEX_HOOK_COMMAND_PARTS),
                }
            ]
        }
    ],
}


def _backup(path: Path) -> None:
    """Copy `path` to a timestamped .bak- sibling with the same (or tighter)
    permissions as the original. Preserves 0600 where applicable so a
    backup of a secrets-bearing file doesn't leak to group/world."""
    if path.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_suffix(path.suffix + f".bak-{ts}")
        content = path.read_bytes()
        src_mode = path.stat().st_mode & 0o777
        # Default to 0600 unless the original was already stricter.
        mode = min(src_mode, 0o600) if src_mode else 0o600
        fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)


def _entry_has_our_hook(entry: dict[str, Any]) -> bool:
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        if isinstance(cmd, str) and "agent-notify hook" in cmd:
            return True
    return False


def _has_our_hook(entries: list[dict[str, Any]]) -> bool:
    return any(_entry_has_our_hook(entry) for entry in entries)


def merge_claude_hooks(settings: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return (new_settings, events_added). Idempotent."""
    hooks = settings.setdefault("hooks", {})
    added: list[str] = []
    for event, new_entries in CLAUDE_HOOK_ENTRIES.items():
        existing = hooks.setdefault(event, [])
        if _has_our_hook(existing):
            continue
        existing.extend(new_entries)
        added.append(event)
    return settings, added


def install_claude_code(settings_path: Path | None = None) -> list[str]:
    settings_path = settings_path or (Path.home() / ".claude" / "settings.json")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings: dict[str, Any] = {}
    if settings_path.exists():
        text = settings_path.read_text() or "{}"
        settings = json.loads(text)
    _backup(settings_path)
    new_settings, added = merge_claude_hooks(settings)
    # settings.json isn't secret, but it names the external command the agent
    # runs on every hook — tighten to owner-only anyway to match the rest of
    # our posture.
    paths.write_secure(settings_path, json.dumps(new_settings, indent=2) + "\n")
    return added


def merge_claude_posttooluse(settings: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Add the PostToolUse back-fill hook for AskUserQuestion / ExitPlanMode.
    Idempotent.

    Cleans up older `agent-notify hook` PostToolUse entries (e.g. with a
    different matcher) before appending the canonical one — third-party
    PostToolUse entries from other tools are left intact.
    """
    hooks = settings.setdefault("hooks", {})
    added: list[str] = []
    desired_entries = CLAUDE_POSTTOOLUSE_ENTRIES["PostToolUse"]
    existing = hooks.get("PostToolUse", [])
    if any(e == desired_entries[0] for e in existing):
        return settings, added
    cleaned_existing = [e for e in existing if not _entry_has_our_hook(e)]
    hooks["PostToolUse"] = cleaned_existing + list(desired_entries)
    added.append("PostToolUse")
    return settings, added


def merge_claude_permissionrequest(settings: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Add the PermissionRequest blocking hook. Idempotent.

    Also migrates legacy installs:
      - Removes any of our previous PreToolUse blocks (Phase 1 hook event).
      - Replaces any of our older PermissionRequest entries (e.g. without the
        timeout from when PermissionRequest was a notification-only hook)
        with the canonical timeout-bearing entry.

    "Our" entries are detected by command substring `agent-notify hook` —
    the user's hand-edited entries are left alone.
    """
    hooks = settings.setdefault("hooks", {})
    added: list[str] = []

    # Migration: drop our legacy PreToolUse entries. The Phase 1 design used
    # PreToolUse with matcher "*" — that fired on every tool call (including
    # auto-allowed reads/grep/task tools), creating noise and tripping
    # Claude Code's "trust this hook for X tool" prompt. PermissionRequest
    # subsumes the use case cleanly: it only fires when the user would have
    # been prompted anyway.
    pretooluse = hooks.get("PreToolUse")
    if pretooluse:
        cleaned = [e for e in pretooluse if not _entry_has_our_hook(e)]
        if cleaned:
            hooks["PreToolUse"] = cleaned
        else:
            del hooks["PreToolUse"]

    desired_entries = CLAUDE_PERMISSIONREQUEST_ENTRIES["PermissionRequest"]
    existing = hooks.get("PermissionRequest", [])
    # Idempotency: if the canonical entry is already present, no-op.
    if any(e == desired_entries[0] for e in existing):
        return settings, added
    # Drop any of our older PermissionRequest entries before appending the
    # canonical one — keeps third-party entries (matching neither command
    # substring nor shape) intact.
    cleaned_existing = [e for e in existing if not _entry_has_our_hook(e)]
    hooks["PermissionRequest"] = cleaned_existing + list(desired_entries)
    added.append("PermissionRequest")
    return settings, added


LAUNCHD_LABEL = "app.coding-agent-notifier.daemon"

# Pre-rename label. Kept so installs older than the rename can be cleanly
# migrated: on install we boot out and unlink the old plist before writing
# the new one, otherwise the user ends up with two daemons fighting over
# the same Socket Mode token.
_LEGACY_LAUNCHD_LABELS: tuple[str, ...] = (
    "com.chrisballinger.agent-notify-daemon",
)


def install_slack_bot(
    settings_path: Path | None = None,
    *,
    launch_agents_dir: Path | None = None,
    agent_notify_bin: str = "agent-notify",
    install_plist: bool = True,
) -> dict[str, Any]:
    """Install the PermissionRequest blocking hook into Claude Code settings
    and (optionally) a launchd plist that supervises `agent-notify daemon`.

    Returns a summary dict with keys:
      - `claude_hooks_added`: list of Claude hook event names added.
      - `plist_path`: path of the launchd plist (whether newly written or
        pre-existing), or None if `install_plist=False`.
      - `plist_written`: bool, True if we actually wrote the plist (it was
        missing or differed from the desired content).
    """
    settings_path = settings_path or (Path.home() / ".claude" / "settings.json")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings: dict[str, Any] = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text() or "{}")
    _backup(settings_path)
    # Include the base hooks too — running install-slack-bot on a fresh
    # install shouldn't require a prior `install claude-code`.
    settings, base_added = merge_claude_hooks(settings)
    settings, pre_added = merge_claude_permissionrequest(settings)
    settings, post_added = merge_claude_posttooluse(settings)
    paths.write_secure(settings_path, json.dumps(settings, indent=2) + "\n")
    claude_added = base_added + pre_added + post_added

    summary: dict[str, Any] = {
        "claude_hooks_added": claude_added,
        "plist_path": None,
        "plist_written": False,
    }

    if install_plist:
        la_dir = launch_agents_dir or (Path.home() / "Library" / "LaunchAgents")
        la_dir.mkdir(parents=True, exist_ok=True)
        legacy_removed = _remove_legacy_plists(la_dir)
        if legacy_removed:
            summary["legacy_plists_removed"] = legacy_removed
        plist_path = la_dir / f"{LAUNCHD_LABEL}.plist"
        # Resolve to an absolute path. launchd's PATH (set on the plist's
        # EnvironmentVariables) is /usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin
        # — `uv tool install` puts the binary in ~/.local/bin/, which isn't
        # on that PATH, so a bare "agent-notify" causes EX_CONFIG and the
        # daemon never starts. Default-resolving here keeps the plist
        # robust to the install location without baking in any one path.
        if agent_notify_bin == "agent-notify":
            resolved = shutil.which(agent_notify_bin)
            if resolved:
                agent_notify_bin = resolved
        desired = _launchd_plist_contents(agent_notify_bin)
        write = True
        if plist_path.exists():
            if plist_path.read_text() == desired:
                write = False
        if write:
            _backup(plist_path)
            # launchd reads the plist as the user; 0600 limits visibility to
            # just the owner and contains blast radius if a future version
            # accidentally sticks a token into EnvironmentVariables.
            paths.write_secure(plist_path, desired)
        summary["plist_path"] = plist_path
        summary["plist_written"] = write

    return summary


def _remove_legacy_plists(la_dir: Path) -> list[str]:
    """Boot out and unlink legacy launchd plists from before the rename.

    Two daemons running under different labels would race for the same
    Socket Mode token (Slack rejects duplicate connections), so we have to
    actively remove the old one rather than letting it linger. Best-effort:
    a missing `launchctl` or a bootout failure is fine — the file unlink
    is what actually stops the next session from re-bootstrapping it.

    Returns the list of legacy labels that were cleaned up.
    """
    import shutil as _shutil
    import subprocess as _subprocess

    cleaned: list[str] = []
    launchctl = _shutil.which("launchctl")
    uid = os.getuid() if hasattr(os, "getuid") else None
    for label in _LEGACY_LAUNCHD_LABELS:
        legacy = la_dir / f"{label}.plist"
        if not legacy.exists():
            continue
        if launchctl and uid is not None:
            # Best-effort. If the daemon isn't loaded, bootout returns
            # nonzero — fine; we still want to delete the file.
            try:
                _subprocess.run(
                    [launchctl, "bootout", f"gui/{uid}", str(legacy)],
                    capture_output=True, timeout=5,
                )
            except (OSError, _subprocess.SubprocessError):
                pass
        try:
            legacy.unlink()
        except OSError:
            continue
        cleaned.append(label)
    return cleaned


def _launchd_plist_contents(agent_notify_bin: str) -> str:
    """Minimal launchd plist that keeps `agent-notify daemon` alive.

    RunAtLoad + KeepAlive restarts on crash; ThrottleInterval (the launchd-
    documented minimum is 10s) caps respawn rate so a daemon that fails to
    bootstrap doesn't fork-bomb itself when KeepAlive immediately retries.
    Not using ProcessType=Background because the daemon needs network + may
    spawn child processes for outgoing HTTPS.
    """
    log_path = str(paths.daemon_log())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{agent_notify_bin}</string>
        <string>daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
"""


def install_codex(
    config_path: Path | None = None,
    hooks_path: Path | None = None,
) -> dict[str, Any]:
    """Install Codex notify + hooks.json entries. Returns summary."""
    codex_dir = Path.home() / ".codex"
    config_path = config_path or codex_dir / "config.toml"
    hooks_path = hooks_path or codex_dir / "hooks.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {"config_updated": False, "hooks_added": []}

    # 1. config.toml — set notify and enable codex_hooks feature
    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text())
    else:
        doc = tomlkit.document()
    existing_notify = doc.get("notify")
    desired_notify = list(CODEX_HOOK_COMMAND_PARTS)
    if existing_notify != desired_notify:
        if existing_notify and "agent-notify" not in json.dumps(list(existing_notify)):
            summary["previous_notify"] = list(existing_notify)
        doc["notify"] = desired_notify
        summary["config_updated"] = True
    features = doc.setdefault("features", tomlkit.table())
    if not features.get("codex_hooks"):
        features["codex_hooks"] = True
        summary["config_updated"] = True
    _backup(config_path)
    # Codex's config may contain other user state — preserve tomlkit's
    # round-trip, but tighten permissions afterward.
    config_path.write_text(tomlkit.dumps(doc))
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass

    # 2. hooks.json
    hooks_doc: dict[str, Any] = {}
    if hooks_path.exists():
        hooks_doc = json.loads(hooks_path.read_text() or "{}")
    hooks_root = hooks_doc.setdefault("hooks", {})
    for event, new_entries in CODEX_HOOK_ENTRIES.items():
        existing = hooks_root.setdefault(event, [])
        if _has_our_hook(existing):
            continue
        existing.extend(new_entries)
        summary["hooks_added"].append(event)
    _backup(hooks_path)
    paths.write_secure(hooks_path, json.dumps(hooks_doc, indent=2) + "\n")
    return summary
