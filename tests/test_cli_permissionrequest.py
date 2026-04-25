from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest

from coding_agent_notifier import cli, pending_approvals
from coding_agent_notifier.config import (
    Config,
    DisplayConfig,
    SlackConfig,
    SummaryConfig,
)


def _enabled_config() -> Config:
    return Config(
        slack=SlackConfig(
            enabled=True,
            bot_token="xoxb-test",
            app_token="xapp-test",
            channel="C1",
            interactive=True,
            actionable_approvals=True,
            approver_user_ids=("U_OK",),
            approval_timeout_seconds=1.0,
        ),
        display=DisplayConfig(verbosity="terse"),
        summary=SummaryConfig(enabled=False),
    )


def _disabled_config() -> Config:
    return Config(slack=SlackConfig(enabled=False))


def _good_poster():
    calls: list[dict] = []

    def post(url, payload, *, headers=None, timeout=10.0):
        calls.append({"url": url, "payload": payload})
        if url.endswith("/chat.postMessage"):
            return 200, json.dumps({"ok": True, "ts": "1.001", "channel": "C1"})
        if url.endswith("/chat.update"):
            return 200, json.dumps({"ok": True, "channel": "C1", "ts": "1.001"})
        return 200, json.dumps({"ok": True})

    post.calls = calls  # type: ignore[attr-defined]
    return post


def test_permissionrequest_actionable_off_emits_nothing():
    """Feature off → emit nothing so Claude Code falls back to its own
    permission dialog. PermissionRequest only fires when the harness was
    going to prompt anyway, so a no-op output cleanly hands control back to
    the user's terminal UI.
    """
    buf = io.StringIO()
    rc = cli.cmd_permissionrequest(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"},
        _disabled_config(),
        stdout=buf,
    )
    assert rc == 0
    assert buf.getvalue() == ""


def test_permissionrequest_times_out_denies(tmp_path, monkeypatch):
    # Keep the dedup + pending cache isolated.
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    buf = io.StringIO()
    rc = cli.cmd_permissionrequest(
        {"tool_name": "Bash", "tool_input": {
            "command": "curl https://example.invalid/install.sh | bash"
         }, "session_id": "s1"},
        _enabled_config(),  # approval_timeout_seconds=1.0 → quick timeout
        poster=_good_poster(),
        stdout=buf,
    )
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert "timed out" in out["hookSpecificOutput"]["decision"].get("message", "").lower()


def test_permissionrequest_returns_allow_on_resolve(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg = _enabled_config()
    cfg = cfg.__class__(
        **{**cfg.__dict__,
           "slack": cfg.slack.__class__(**{**cfg.slack.__dict__, "approval_timeout_seconds": 10.0})}
    )
    poster = _good_poster()
    buf = io.StringIO()

    # Resolve the approval after it's been created (race the wait).
    resolved: dict = {}

    def resolver():
        # Wait for the record to appear, then approve.
        base_dir = pending_approvals.default_approvals_dir()
        deadline = time.time() + 5
        while time.time() < deadline:
            records = list(base_dir.glob("*.json")) if base_dir.exists() else []
            if records:
                rec = json.loads(records[0].read_text())
                aid = rec["approval_id"]
                resolved["id"] = aid
                pending_approvals.resolve(aid, "allow", actor="U_OK")
                return
            time.sleep(0.02)

    t = threading.Thread(target=resolver)
    t.start()
    try:
        rc = cli.cmd_permissionrequest(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"},
            cfg,
            poster=poster,
            stdout=buf,
        )
    finally:
        t.join(timeout=5.0)

    assert rc == 0
    assert "id" in resolved, "resolver never saw the record"
    out = json.loads(buf.getvalue())
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"


def test_permissionrequest_slack_post_failure_denies(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))

    def boom(url, payload, *, headers=None, timeout=10.0):
        return 500, "internal error"

    buf = io.StringIO()
    rc = cli.cmd_permissionrequest(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"},
        _enabled_config(),
        poster=boom,
        stdout=buf,
    )
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_permissionrequest_emits_valid_claude_code_json_shape(tmp_path, monkeypatch):
    """When the hook DOES emit a decision (post-failure deny path here), the
    JSON shape must match what Claude Code expects for PermissionRequest:
    `hookSpecificOutput.decision.behavior` (not `permissionDecision`), and
    `decision.message` (not `permissionDecisionReason`).
    """
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))

    def boom(url, payload, *, headers=None, timeout=10.0):
        return 500, "internal error"

    buf = io.StringIO()
    cli.cmd_permissionrequest(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"},
        _enabled_config(),
        poster=boom,
        stdout=buf,
    )
    out = json.loads(buf.getvalue())
    # Claude Code looks for exactly this key path.
    assert "hookSpecificOutput" in out
    assert out["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    # PermissionRequest schema: decision.behavior is allow|deny only.
    assert out["hookSpecificOutput"]["decision"]["behavior"] in ("allow", "deny")
    # The legacy fields must NOT be present.
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert "permissionDecisionReason" not in out["hookSpecificOutput"]


def test_permissionrequest_picks_workspace_by_route_and_stamps_record(tmp_path, monkeypatch):
    """cmd_permissionrequest should resolve the workspace for the payload's
    cwd and persist it on the approval record so the hook's timeout-cleanup
    can look up the right bot_token later."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    from coding_agent_notifier import config as cfgmod

    (tmp_path / "acme").mkdir()

    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "home": {
                    "enabled": True,
                    "bot_token": "xoxb-home",
                    "app_token": "xapp-home",
                    "actionable_approvals": True,
                    "approver_user_ids": ["U_HOME"],
                    "approval_timeout_seconds": 1.0,
                },
                "work": {
                    "enabled": True,
                    "bot_token": "xoxb-work",
                    "app_token": "xapp-work",
                    "actionable_approvals": True,
                    "approver_user_ids": ["U_WORK"],
                    "approval_timeout_seconds": 1.0,
                    "channel": "#agents-work",
                },
            },
        },
        "routes": [
            {"cwd": f"{tmp_path}/acme", "slack": {"workspace": "work"}},
        ],
        "display": {"verbosity": "terse"},
        "summary": {"enabled": False},
    })

    # Stub the Slack API so we can see which bot_token was used.
    used_bot_tokens: list[str] = []

    def post(url, payload, *, headers=None, timeout=10.0):
        auth = (headers or {}).get("Authorization", "")
        if auth.startswith("Bearer "):
            used_bot_tokens.append(auth[len("Bearer "):])
        if url.endswith("/chat.postMessage"):
            return 200, json.dumps({"ok": True, "ts": "1.0", "channel": "C_WORK"})
        if url.endswith("/chat.update"):
            return 200, json.dumps({"ok": True})
        return 200, json.dumps({"ok": True})

    buf = io.StringIO()
    rc = cli.cmd_permissionrequest(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "session_id": "s1",
            "cwd": str(tmp_path / "acme"),
        },
        cfg,
        poster=post,
        stdout=buf,
    )
    assert rc == 0
    # Route selected the "work" workspace, so xoxb-work was used (not home)
    # for both chat.postMessage and the timeout chat.update.
    assert any(t == "xoxb-work" for t in used_bot_tokens)
    assert all(t != "xoxb-home" for t in used_bot_tokens)


def test_permissionrequest_emits_updated_input_for_ask_user_question(tmp_path, monkeypatch):
    """When the user clicks an AskUserQuestion option button, the resolved
    record carries `selected_option_index`. The hook must pre-fill the
    answer via `decision.updatedInput.answers` so AskUserQuestion returns
    immediately instead of prompting in the terminal."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg = _enabled_config()
    # Bump the timeout so the resolver thread has room.
    cfg = cfg.__class__(
        **{**cfg.__dict__,
           "slack": cfg.slack.__class__(**{**cfg.slack.__dict__, "approval_timeout_seconds": 10.0})}
    )
    poster = _good_poster()
    buf = io.StringIO()

    def resolver():
        base_dir = pending_approvals.default_approvals_dir()
        deadline = time.time() + 5
        while time.time() < deadline:
            records = list(base_dir.glob("*.json")) if base_dir.exists() else []
            if records:
                rec = json.loads(records[0].read_text())
                pending_approvals.resolve(
                    rec["approval_id"], "allow",
                    actor="U_OK", selected_option=1,
                )
                return
            time.sleep(0.02)

    t = threading.Thread(target=resolver)
    t.start()
    try:
        rc = cli.cmd_permissionrequest(
            {
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {
                            "question": "How should X be configured?",
                            "options": [
                                {"label": "Global only"},
                                {"label": "Per-repo"},
                                {"label": "Hybrid"},
                            ],
                        }
                    ]
                },
                "session_id": "s1",
            },
            cfg,
            poster=poster,
            stdout=buf,
        )
    finally:
        t.join(timeout=5.0)

    assert rc == 0
    out = json.loads(buf.getvalue())
    decision = out["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    assert decision["updatedInput"] == {
        "answers": {"How should X be configured?": "Per-repo"},
    }


def test_permissionrequest_emits_multi_question_updated_input(tmp_path, monkeypatch):
    """Multi-question AskUserQuestion: when the resolved record carries
    `selected_options` (dict), the hook emits `updatedInput.answers` with
    one entry per answered question."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg = _enabled_config()
    cfg = cfg.__class__(
        **{**cfg.__dict__,
           "slack": cfg.slack.__class__(**{**cfg.slack.__dict__, "approval_timeout_seconds": 10.0})}
    )
    poster = _good_poster()
    buf = io.StringIO()

    def resolver():
        base_dir = pending_approvals.default_approvals_dir()
        deadline = time.time() + 5
        while time.time() < deadline:
            records = list(base_dir.glob("*.json")) if base_dir.exists() else []
            if records:
                rec = json.loads(records[0].read_text())
                pending_approvals.resolve(
                    rec["approval_id"], "allow",
                    actor="U_OK",
                    selected_options={"0": 0, "1": 1},
                )
                return
            time.sleep(0.02)

    t = threading.Thread(target=resolver)
    t.start()
    try:
        rc = cli.cmd_permissionrequest(
            {
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {
                            "question": "Mascot?",
                            "options": [{"label": "Raccoon"}, {"label": "Capybara"}],
                        },
                        {
                            "question": "Color?",
                            "options": [{"label": "Green"}, {"label": "Yellow"}],
                        },
                    ]
                },
                "session_id": "s1",
            },
            cfg,
            poster=poster,
            stdout=buf,
        )
    finally:
        t.join(timeout=5.0)

    assert rc == 0
    out = json.loads(buf.getvalue())
    decision = out["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    assert decision["updatedInput"] == {
        "answers": {"Mascot?": "Raccoon", "Color?": "Yellow"},
    }


def test_permissionrequest_no_updated_input_for_plain_approve(tmp_path, monkeypatch):
    """Approve clicks (not option clicks) on a non-AskUserQuestion tool emit
    `behavior: allow` with NO `updatedInput` field — we don't modify the
    tool's parameters in that case."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg = _enabled_config()
    cfg = cfg.__class__(
        **{**cfg.__dict__,
           "slack": cfg.slack.__class__(**{**cfg.slack.__dict__, "approval_timeout_seconds": 10.0})}
    )
    poster = _good_poster()
    buf = io.StringIO()

    def resolver():
        base_dir = pending_approvals.default_approvals_dir()
        deadline = time.time() + 5
        while time.time() < deadline:
            records = list(base_dir.glob("*.json")) if base_dir.exists() else []
            if records:
                rec = json.loads(records[0].read_text())
                pending_approvals.resolve(rec["approval_id"], "allow", actor="U_OK")
                return
            time.sleep(0.02)

    t = threading.Thread(target=resolver)
    t.start()
    try:
        rc = cli.cmd_permissionrequest(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"},
            cfg,
            poster=poster,
            stdout=buf,
        )
    finally:
        t.join(timeout=5.0)

    assert rc == 0
    out = json.loads(buf.getvalue())
    decision = out["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    assert "updatedInput" not in decision


def test_permissionrequest_emits_updated_permissions_for_suggestion(tmp_path, monkeypatch):
    """When the user clicks a permission_suggestion button, the resolved
    record carries `selected_suggestion_index`. The hook emits
    `decision.updatedPermissions` with that single suggestion so Claude
    Code applies the rule edit."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg = _enabled_config()
    cfg = cfg.__class__(
        **{**cfg.__dict__,
           "slack": cfg.slack.__class__(**{**cfg.slack.__dict__, "approval_timeout_seconds": 10.0})}
    )
    poster = _good_poster()
    buf = io.StringIO()

    def resolver():
        base_dir = pending_approvals.default_approvals_dir()
        deadline = time.time() + 5
        while time.time() < deadline:
            records = list(base_dir.glob("*.json")) if base_dir.exists() else []
            if records:
                rec = json.loads(records[0].read_text())
                pending_approvals.resolve(
                    rec["approval_id"], "allow",
                    actor="U_OK",
                    selected_suggestion=0,
                )
                return
            time.sleep(0.02)

    suggestion = {
        "type": "addRules",
        "rules": [{"toolName": "Bash", "ruleContent": "curl:*"}],
        "behavior": "allow",
        "destination": "localSettings",
    }

    t = threading.Thread(target=resolver)
    t.start()
    try:
        rc = cli.cmd_permissionrequest(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "curl https://example.invalid"},
                "session_id": "s1",
                "permission_suggestions": [suggestion],
            },
            cfg,
            poster=poster,
            stdout=buf,
        )
    finally:
        t.join(timeout=5.0)

    assert rc == 0
    out = json.loads(buf.getvalue())
    decision = out["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    assert decision["updatedPermissions"] == [suggestion]
    # No updatedInput on a suggestion click — only the rule edit is applied.
    assert "updatedInput" not in decision


def test_emit_decision_drops_updated_input_when_decision_is_deny():
    """`updatedInput` is allow-only per the PermissionRequest schema —
    silently dropped on deny so we never leak hook intent into a deny path."""
    buf = io.StringIO()
    cli._emit_decision(buf, "deny", reason="nope", updated_input={"x": 1})
    out = json.loads(buf.getvalue())
    assert "updatedInput" not in out["hookSpecificOutput"]["decision"]
    assert out["hookSpecificOutput"]["decision"]["message"] == "nope"


def test_permissionrequest_emits_freeform_answer_in_updated_input(tmp_path, monkeypatch):
    """When the user submits a custom-answer modal, the typed string lands
    in `decision.updatedInput.answers["<question>"]` instead of an option
    label."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg = _enabled_config()
    cfg = cfg.__class__(
        **{**cfg.__dict__,
           "slack": cfg.slack.__class__(**{**cfg.slack.__dict__, "approval_timeout_seconds": 10.0})}
    )
    poster = _good_poster()
    buf = io.StringIO()

    def resolver():
        base_dir = pending_approvals.default_approvals_dir()
        deadline = time.time() + 5
        while time.time() < deadline:
            records = list(base_dir.glob("*.json")) if base_dir.exists() else []
            if records:
                rec = json.loads(records[0].read_text())
                pending_approvals.resolve(
                    rec["approval_id"], "allow",
                    actor="U_OK",
                    freeform_answers={"0": "A pangolin"},
                )
                return
            time.sleep(0.02)

    t = threading.Thread(target=resolver)
    t.start()
    try:
        rc = cli.cmd_permissionrequest(
            {
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [{
                        "question": "Mascot?",
                        "options": [{"label": "Raccoon"}, {"label": "Capybara"}],
                    }],
                },
                "session_id": "s1",
            },
            cfg, poster=poster, stdout=buf,
        )
    finally:
        t.join(timeout=5.0)

    assert rc == 0
    decision = json.loads(buf.getvalue())["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    assert decision["updatedInput"] == {"answers": {"Mascot?": "A pangolin"}}


def test_permissionrequest_freeform_wins_over_option_per_question(tmp_path, monkeypatch):
    """Mixed multi-Q resolution: freeform text on Q1 wins over any option
    index, while Q2's option label still surfaces normally."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg = _enabled_config()
    cfg = cfg.__class__(
        **{**cfg.__dict__,
           "slack": cfg.slack.__class__(**{**cfg.slack.__dict__, "approval_timeout_seconds": 10.0})}
    )
    poster = _good_poster()
    buf = io.StringIO()

    def resolver():
        base_dir = pending_approvals.default_approvals_dir()
        deadline = time.time() + 5
        while time.time() < deadline:
            records = list(base_dir.glob("*.json")) if base_dir.exists() else []
            if records:
                rec = json.loads(records[0].read_text())
                pending_approvals.resolve(
                    rec["approval_id"], "allow",
                    actor="U_OK",
                    selected_options={"1": 1},  # Color = Yellow
                    freeform_answers={"0": "A pangolin"},
                )
                return
            time.sleep(0.02)

    t = threading.Thread(target=resolver)
    t.start()
    try:
        rc = cli.cmd_permissionrequest(
            {
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {"question": "Mascot?", "options": [{"label": "Raccoon"}, {"label": "Capybara"}]},
                        {"question": "Color?", "options": [{"label": "Green"}, {"label": "Yellow"}]},
                    ],
                },
                "session_id": "s1",
            },
            cfg, poster=poster, stdout=buf,
        )
    finally:
        t.join(timeout=5.0)

    assert rc == 0
    decision = json.loads(buf.getvalue())["hookSpecificOutput"]["decision"]
    assert decision["updatedInput"] == {
        "answers": {"Mascot?": "A pangolin", "Color?": "Yellow"},
    }


def test_permissionrequest_emits_deny_reason_as_decision_message(tmp_path, monkeypatch):
    """Deny resolution with deny_reason puts the typed text into
    `decision.message` so Claude sees it as the rejection reason."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg = _enabled_config()
    cfg = cfg.__class__(
        **{**cfg.__dict__,
           "slack": cfg.slack.__class__(**{**cfg.slack.__dict__, "approval_timeout_seconds": 10.0})}
    )
    poster = _good_poster()
    buf = io.StringIO()

    def resolver():
        base_dir = pending_approvals.default_approvals_dir()
        deadline = time.time() + 5
        while time.time() < deadline:
            records = list(base_dir.glob("*.json")) if base_dir.exists() else []
            if records:
                rec = json.loads(records[0].read_text())
                pending_approvals.resolve(
                    rec["approval_id"], "deny",
                    actor="U_OK",
                    deny_reason="check the lockfile first",
                )
                return
            time.sleep(0.02)

    t = threading.Thread(target=resolver)
    t.start()
    try:
        rc = cli.cmd_permissionrequest(
            {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "session_id": "s1"},
            cfg, poster=poster, stdout=buf,
        )
    finally:
        t.join(timeout=5.0)

    assert rc == 0
    decision = json.loads(buf.getvalue())["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
    assert decision["message"] == "check the lockfile first"


def test_permissionrequest_deny_without_reason_leaves_message_unset(tmp_path, monkeypatch):
    """One-tap deny (no modal) → decision has no `message` field."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    cfg = _enabled_config()
    cfg = cfg.__class__(
        **{**cfg.__dict__,
           "slack": cfg.slack.__class__(**{**cfg.slack.__dict__, "approval_timeout_seconds": 10.0})}
    )
    poster = _good_poster()
    buf = io.StringIO()

    def resolver():
        base_dir = pending_approvals.default_approvals_dir()
        deadline = time.time() + 5
        while time.time() < deadline:
            records = list(base_dir.glob("*.json")) if base_dir.exists() else []
            if records:
                rec = json.loads(records[0].read_text())
                pending_approvals.resolve(rec["approval_id"], "deny", actor="U_OK")
                return
            time.sleep(0.02)

    t = threading.Thread(target=resolver)
    t.start()
    try:
        rc = cli.cmd_permissionrequest(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"},
            cfg, poster=poster, stdout=buf,
        )
    finally:
        t.join(timeout=5.0)

    assert rc == 0
    decision = json.loads(buf.getvalue())["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
    assert "message" not in decision


def test_permissionrequest_strict_routing_no_match_emits_nothing(tmp_path, monkeypatch):
    """If routes are configured and none match the cwd, strict mode returns
    None from sinks_for. The hook must emit nothing so Claude Code falls
    back to its own permission dialog — PermissionRequest only fires when
    the harness was about to prompt anyway, so a no-op is the correct
    pass-through."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    from coding_agent_notifier import config as cfgmod

    cfg = cfgmod.parse_config({
        "slack": {
            "workspaces": {
                "work": {
                    "enabled": True,
                    "bot_token": "xoxb",
                    "app_token": "xapp",
                    "actionable_approvals": True,
                    "approver_user_ids": ["U"],
                },
            },
        },
        "routes": [
            {"cwd": "/nonexistent/*", "slack": {"workspace": "work"}},
        ],
    })

    buf = io.StringIO()
    rc = cli.cmd_permissionrequest(
        {"tool_name": "Bash", "cwd": str(tmp_path)},
        cfg,
        stdout=buf,
    )
    assert rc == 0
    assert buf.getvalue() == ""
