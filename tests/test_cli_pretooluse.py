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


def test_pretooluse_actionable_off_emits_ask():
    buf = io.StringIO()
    rc = cli.cmd_pretooluse(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"},
        _disabled_config(),
        stdout=buf,
    )
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_pretooluse_times_out_denies(tmp_path, monkeypatch):
    # Keep the dedup + pending cache isolated.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    buf = io.StringIO()
    rc = cli.cmd_pretooluse(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}, "session_id": "s1"},
        _enabled_config(),  # approval_timeout_seconds=1.0 → quick timeout
        poster=_good_poster(),
        stdout=buf,
    )
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "timed out" in out["hookSpecificOutput"].get("permissionDecisionReason", "").lower()


def test_pretooluse_returns_allow_on_resolve(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
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
        rc = cli.cmd_pretooluse(
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
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_pretooluse_slack_post_failure_denies(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    def boom(url, payload, *, headers=None, timeout=10.0):
        return 500, "internal error"

    buf = io.StringIO()
    rc = cli.cmd_pretooluse(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"},
        _enabled_config(),
        poster=boom,
        stdout=buf,
    )
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretooluse_emits_valid_claude_code_json_shape():
    buf = io.StringIO()
    cli.cmd_pretooluse({"tool_name": "Bash"}, _disabled_config(), stdout=buf)
    out = json.loads(buf.getvalue())
    # Claude Code looks for exactly this key path.
    assert "hookSpecificOutput" in out
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert out["hookSpecificOutput"]["permissionDecision"] in ("allow", "deny", "ask")
