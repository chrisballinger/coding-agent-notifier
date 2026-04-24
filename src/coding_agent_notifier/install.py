from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import tomlkit

CLAUDE_HOOK_COMMAND = "agent-notify hook --source claude-code"
CODEX_HOOK_COMMAND_PARTS = ["agent-notify", "hook", "--source", "codex"]

CLAUDE_HOOK_ENTRIES: dict[str, list[dict[str, Any]]] = {
    "Notification": [
        {
            "matcher": "permission_prompt|idle_prompt|elicitation_dialog",
            "hooks": [{"type": "command", "command": CLAUDE_HOOK_COMMAND}],
        }
    ],
    "PermissionRequest": [
        {
            "matcher": "*",
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
    if path.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        path.with_suffix(path.suffix + f".bak-{ts}").write_bytes(path.read_bytes())


def _has_our_hook(entries: list[dict[str, Any]]) -> bool:
    for entry in entries:
        for h in entry.get("hooks", []):
            cmd = h.get("command", "")
            if isinstance(cmd, str) and "agent-notify hook" in cmd:
                return True
    return False


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
    settings_path.write_text(json.dumps(new_settings, indent=2) + "\n")
    return added


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
    config_path.write_text(tomlkit.dumps(doc))

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
    hooks_path.write_text(json.dumps(hooks_doc, indent=2) + "\n")
    return summary
