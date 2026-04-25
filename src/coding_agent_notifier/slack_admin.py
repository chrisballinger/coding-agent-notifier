"""Interactive `agent-notify slack` subcommands — add/list/remove/test.

Separates the wizard from cli.py so the I/O (stdin prompts, Keychain
writes, tomlkit round-trip, HTTP verification) stays testable and the
CLI layer stays a thin argparse dispatcher.

The wizard's contract:
  1. Collect tokens + channel + approver IDs (interactive prompts OR
     non-interactive `--flag` values).
  2. Verify the bot token by calling `auth.test` (unless --no-verify).
  3. Store tokens in macOS Keychain (idempotent via `-U`).
  4. Upsert the `[slack.workspaces.<name>]` block in `config.toml` via
     tomlkit, preserving user comments on unrelated keys.
  5. Print a summary + any security warnings (shared channel + empty
     allowlist would fail at next load).

Rollback: if Keychain writes succeed but the config.toml write fails,
the wizard attempts to delete the just-written Keychain entries so a
half-applied workspace can't linger. Fully atomic is not achievable
(two separate storage backends) but the best-effort rollback covers
the common partial-failure case.
"""
from __future__ import annotations

import getpass
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable

import tomlkit
from tomlkit import TOMLDocument

from . import keychain, paths
from .config import ConfigError, load_config
from .sinks.base import http_post_json

SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"
SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


@dataclass
class WorkspaceInfo:
    name: str
    enabled: bool
    has_bot_token: bool
    has_app_token: bool
    interactive: bool
    actionable_approvals: bool
    channel: str | None
    approver_user_ids: tuple[str, ...]
    approver_user_groups: tuple[str, ...]


# ---------------------------------------------------------------------
# `slack add` — interactive wizard + non-interactive scripted mode
# ---------------------------------------------------------------------


def run_add_wizard(
    *,
    name: str | None = None,
    bot_token: str | None = None,
    app_token: str | None = None,
    channel: str | None = None,
    approvers: str | None = None,
    no_verify: bool = False,
    no_actionable: bool = False,
    config_path: Path | None = None,
    stdin: IO | None = None,
    stdout: IO | None = None,
    stderr: IO | None = None,
    poster: Callable | None = None,
    prompt_password: Callable[[str], str] | None = None,
) -> int:
    """Run the `agent-notify slack add` flow. Returns an exit code.

    All I/O is injectable so tests can drive the wizard deterministically.
    `prompt_password` defaults to `getpass.getpass` and is only replaced in
    tests.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    poster = poster or http_post_json
    prompt_password = prompt_password or getpass.getpass
    config_path = config_path or paths.config_file()

    # 1. Workspace name
    if name is None:
        name = _prompt(stdin, stdout, "Workspace name [default]: ", "default")
    name = name.strip()
    if not name:
        print("error: workspace name cannot be empty", file=stderr)
        return 1

    # 2. Bot token (xoxb-*)
    bot_token = _resolve_token_input(
        bot_token, stdin, "Bot token (xoxb-…): ", prompt_password,
    )
    if not bot_token:
        print("error: bot token is required", file=stderr)
        return 1
    if not bot_token.startswith("xoxb-"):
        print(
            f"warning: bot token doesn't start with 'xoxb-' — continuing, but "
            f"Slack will likely reject auth.test",
            file=stderr,
        )

    # 3. App token (xapp-*) — optional; only needed for actionable approvals
    app_token = _resolve_token_input(
        app_token, stdin,
        "App-level token (xapp-…, enables interactive buttons; blank to skip): ",
        prompt_password,
        allow_blank=True,
    )
    if app_token and not app_token.startswith("xapp-"):
        print(
            f"warning: app token doesn't start with 'xapp-' — continuing, but "
            f"Slack Socket Mode will likely reject it",
            file=stderr,
        )

    # 4. Verify with Slack's auth.test
    if not no_verify:
        ok, err_or_team = _verify_bot_token(bot_token, poster=poster)
        if not ok:
            print(f"error: auth.test failed: {err_or_team}", file=stderr)
            return 1
        print(f"verified: team {err_or_team!r}", file=stdout)

    # 5. Channel (default @me — DM with the bot)
    if channel is None:
        channel = _prompt(
            stdin, stdout,
            "Channel [@me = DM with the bot, recommended]: ", "@me",
        )
    channel = channel.strip() or "@me"

    # 6. Approver user IDs — comma-separated
    if approvers is None:
        hint = (
            "leave blank for DM-only (secure)"
            if channel == "@me" else
            "required — comma-separated Slack user IDs (U…)"
        )
        approvers = _prompt(
            stdin, stdout,
            f"Approver user IDs ({hint}): ",
            "",
        )
    approver_list = [a.strip() for a in approvers.split(",") if a.strip()]

    # 7. Store tokens in Keychain
    bot_account = keychain.account_for(name, "bot_token")
    app_account = keychain.account_for(name, "app_token")
    stored_accounts: list[str] = []
    try:
        keychain.write(bot_account, bot_token)
        stored_accounts.append(bot_account)
        if app_token:
            keychain.write(app_account, app_token)
            stored_accounts.append(app_account)
    except (keychain.KeychainError, ValueError) as e:
        print(f"error: Keychain write failed: {e}", file=stderr)
        _rollback_keychain(stored_accounts)
        return 1

    # 8. Build workspace block + upsert into config.toml
    block: dict[str, Any] = {
        "enabled": True,
        "bot_token_keychain": bot_account,
        "channel": channel,
    }
    if app_token:
        block["app_token_keychain"] = app_account
        block["interactive"] = True
        if not no_actionable:
            block["actionable_approvals"] = True
    if approver_list:
        block["approver_user_ids"] = approver_list

    try:
        _upsert_workspace_in_config(config_path, name, block)
    except (OSError, ConfigError) as e:
        print(f"error: writing {config_path} failed: {e}", file=stderr)
        _rollback_keychain(stored_accounts)
        return 1

    # 9. Summary + security warnings
    print(f"\n✓ wrote [slack.workspaces.{name}] to {config_path}", file=stdout)
    for acc in stored_accounts:
        print(f"  stored secret at keychain agent-notify:{acc}", file=stdout)
    if block.get("actionable_approvals"):
        if not approver_list and channel != "@me":
            print(
                f"\nwarning: channel={channel!r} is not a DM and no approver_user_ids "
                f"were set. The next config load will reject actionable_approvals=true "
                f"here. Re-run `agent-notify slack add --name {name} --approvers <U…>` "
                f"to fix.",
                file=stderr,
            )
    return 0


# ---------------------------------------------------------------------
# `slack list`
# ---------------------------------------------------------------------


def list_workspaces(config_path: Path | None = None) -> list[WorkspaceInfo]:
    """Load the config and project each workspace to a small info record.

    Load errors bubble up (ConfigError) — caller decides how to display.
    """
    cfg = load_config(config_path) if config_path else load_config()
    out: list[WorkspaceInfo] = []
    for name, ws in cfg.slack_workspaces.items():
        out.append(WorkspaceInfo(
            name=name,
            enabled=ws.enabled,
            has_bot_token=bool(ws.bot_token),
            has_app_token=bool(ws.app_token),
            interactive=ws.interactive,
            actionable_approvals=ws.actionable_approvals,
            channel=ws.channel,
            approver_user_ids=ws.approver_user_ids,
            approver_user_groups=ws.approver_user_groups,
        ))
    return out


# ---------------------------------------------------------------------
# `slack remove <name>`
# ---------------------------------------------------------------------


def remove_workspace(
    name: str,
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Delete the workspace block from config.toml and clear its Keychain
    entries. Returns a summary of what actually changed.

    Idempotent: re-running on a missing workspace returns
    `{"config_removed": False, "keychain_removed": []}`.
    """
    config_path = config_path or paths.config_file()
    config_removed = False
    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text())
        workspaces = _get_workspaces_table(doc)
        if workspaces is not None and name in workspaces:
            del workspaces[name]
            _write_secure_tomlkit(config_path, doc)
            config_removed = True

    keychain_removed: list[str] = []
    for field in ("bot_token", "app_token"):
        account = keychain.account_for(name, field)
        try:
            if keychain.delete(account):
                keychain_removed.append(account)
        except keychain.KeychainError:
            # Missing binary, timeout, etc. — surface to caller via the
            # summary. Better than silent failure; better than raising
            # mid-cleanup when the config-side work already succeeded.
            pass
    return {"config_removed": config_removed, "keychain_removed": keychain_removed}


# ---------------------------------------------------------------------
# `slack test <name>`
# ---------------------------------------------------------------------


def test_workspace(
    name: str,
    *,
    config_path: Path | None = None,
    poster: Callable | None = None,
) -> tuple[bool, str]:
    """Post a synthetic message to the named workspace. Returns (ok, msg)."""
    poster = poster or http_post_json
    cfg = load_config(config_path) if config_path else load_config()
    ws = cfg.slack_workspaces.get(name)
    if ws is None:
        return False, f"unknown workspace {name!r}"
    if not ws.bot_token:
        return False, f"workspace {name!r} has no resolved bot_token"
    channel = ws.channel or "@me"
    if channel == "@me":
        from .sinks.slack import _dm_target, SinkError
        try:
            channel = _dm_target(ws, poster=poster)
        except SinkError as e:
            return False, f"_dm_target failed: {e}"
    payload = {
        "channel": channel,
        "text": f":wave: agent-notify smoke test — workspace={name}",
    }
    status, text = poster(
        SLACK_POST_MESSAGE_URL,
        payload,
        headers={"Authorization": f"Bearer {ws.bot_token}"},
    )
    if status >= 300:
        return False, f"HTTP {status}: {text!r}"
    try:
        parsed = json.loads(text)
    except ValueError:
        return False, f"non-JSON response: {text!r}"
    if not parsed.get("ok"):
        return False, f"Slack API error: {parsed.get('error', 'unknown')}"
    return True, f"posted to {channel}"


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _prompt(stdin: IO, stdout: IO, prompt: str, default: str) -> str:
    stdout.write(prompt)
    stdout.flush()
    line = stdin.readline().rstrip("\n")
    return line if line else default


def _resolve_token_input(
    cli_value: str | None,
    stdin: IO,
    prompt: str,
    prompt_password: Callable[[str], str],
    *,
    allow_blank: bool = False,
) -> str:
    """Given a CLI-provided token (None, '-', or literal value), return the
    final token string. '-' reads a line from stdin; None prompts with
    password-style echo suppression."""
    if cli_value == "-":
        return stdin.readline().strip()
    if cli_value is not None:
        return cli_value
    value = prompt_password(prompt).strip()
    if not value and not allow_blank:
        return ""
    return value


def _verify_bot_token(
    bot_token: str,
    *,
    poster: Callable,
) -> tuple[bool, str]:
    """Call Slack auth.test; return (ok, team_name_or_error)."""
    try:
        status, text = poster(
            SLACK_AUTH_TEST_URL,
            {},
            headers={"Authorization": f"Bearer {bot_token}"},
        )
    except Exception as e:  # noqa: BLE001 - network failures surface as err text
        return False, f"HTTP request raised: {e}"
    if status >= 300:
        return False, f"HTTP {status}: {text!r}"
    try:
        parsed = json.loads(text)
    except ValueError:
        return False, f"non-JSON response: {text!r}"
    if not parsed.get("ok"):
        return False, parsed.get("error", "unknown")
    return True, parsed.get("team") or parsed.get("team_id") or "(team)"


def _rollback_keychain(accounts: list[str]) -> None:
    for acc in accounts:
        try:
            keychain.delete(acc)
        except keychain.KeychainError:
            pass


def _upsert_workspace_in_config(
    config_path: Path,
    name: str,
    fields: dict[str, Any],
) -> None:
    """Add-or-replace `[slack.workspaces.<name>]` in config.toml. Preserves
    any other tables, comments, and formatting via tomlkit round-trip.
    Writes with 0600 perms via `paths.write_secure`."""
    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text())
    else:
        doc = tomlkit.document()

    slack = doc.get("slack")
    if not isinstance(slack, dict):
        slack = tomlkit.table()
        doc["slack"] = slack
    workspaces = slack.get("workspaces")
    if not isinstance(workspaces, dict):
        workspaces = tomlkit.table()
        slack["workspaces"] = workspaces

    # Build the workspace block fresh every time so re-running the wizard
    # (after e.g. rotating a token) drops stale keys rather than leaving
    # them behind.
    block = tomlkit.table()
    for key, value in fields.items():
        if isinstance(value, list):
            arr = tomlkit.array()
            for item in value:
                arr.append(item)
            block[key] = arr
        else:
            block[key] = value
    workspaces[name] = block

    _write_secure_tomlkit(config_path, doc)


def _get_workspaces_table(doc: TOMLDocument):
    slack = doc.get("slack")
    if not isinstance(slack, dict):
        return None
    workspaces = slack.get("workspaces")
    if not isinstance(workspaces, dict):
        return None
    return workspaces


def _write_secure_tomlkit(path: Path, doc: TOMLDocument) -> None:
    paths.write_secure(path, tomlkit.dumps(doc))
