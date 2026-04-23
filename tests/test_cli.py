from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from coding_agent_notifier import cli


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    out = capsys.readouterr().out
    assert "agent-notify" in out


def test_config_path_prints(tmp_path: Path, capsys, monkeypatch):
    cfg = tmp_path / "c.toml"
    assert cli.main(["--config", str(cfg), "config", "path"]) == 0
    assert str(cfg) in capsys.readouterr().out


def test_config_init_writes_file(tmp_path: Path):
    cfg = tmp_path / "c.toml"
    assert cli.main(["--config", str(cfg), "config", "init"]) == 0
    assert cfg.exists()
    # Writing again should fail (not overwrite)
    rc = cli.main(["--config", str(cfg), "config", "init"])
    assert rc == 1


def test_hook_with_empty_stdin_is_noop(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["hook", "--source", "claude-code"]) == 0


def test_hook_with_malformed_json_prints_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cli.main(["hook", "--source", "claude-code"]) == 0
    err = capsys.readouterr().err
    assert "malformed" in err.lower()


def test_hook_dispatches_through_sink(monkeypatch, tmp_path: Path):
    # enabled slack sink, always gating, fake http
    cfg = _write_config(
        tmp_path,
        """
gating = "always"
[sinks.slack]
enabled = true
webhook_url = "https://hook.test/x"
""".strip(),
    )
    calls = []

    def fake_post(url, body, headers=None, timeout=10.0):
        calls.append((url, body, headers))
        return 200, "ok"

    monkeypatch.setattr("coding_agent_notifier.sinks.slack.http_post_json", fake_post)
    payload = {
        "hook_event_name": "Stop",
        "cwd": "/tmp/x",
        "session_id": "sess-1",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    # Avoid real macOS calls
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0.0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")
    rc = cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0] == "https://hook.test/x"


def test_hook_suppressed_by_gating(monkeypatch, tmp_path: Path):
    cfg = _write_config(
        tmp_path,
        """
gating = "idle_only"
idle_threshold_seconds = 9999
[sinks.slack]
enabled = true
webhook_url = "https://hook.test/x"
""".strip(),
    )
    calls = []

    def fake_post(*a, **k):
        calls.append(a)
        return 200, "ok"

    monkeypatch.setattr("coding_agent_notifier.sinks.slack.http_post_json", fake_post)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0.0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")
    payload = {"hook_event_name": "Stop", "cwd": "/tmp", "session_id": "s"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])
    assert calls == []


def test_hook_unknown_payload_returns_none(monkeypatch, tmp_path: Path):
    cfg = _write_config(tmp_path, "")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart"})))
    # Should simply return 0 without error
    rc = cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])
    assert rc == 0


def test_install_claude_code(monkeypatch, tmp_path: Path):
    target = tmp_path / "settings.json"

    def fake_install(_path=None):
        target.write_text("{}")
        return ["Notification"]

    monkeypatch.setattr("coding_agent_notifier.cli.install.install_claude_code", fake_install)
    rc = cli.main(["install", "claude-code"])
    assert rc == 0


def test_install_codex(monkeypatch):
    def fake_install(*_a, **_k):
        return {"config_updated": True, "hooks_added": ["Stop"]}
    monkeypatch.setattr("coding_agent_notifier.cli.install.install_codex", fake_install)
    rc = cli.main(["install", "codex"])
    assert rc == 0


def test_test_subcommand_with_force(monkeypatch, tmp_path: Path):
    cfg = _write_config(
        tmp_path,
        """
[sinks.slack]
enabled = true
webhook_url = "https://hook.test/x"
""".strip(),
    )
    calls = []

    def fake_post(*a, **k):
        calls.append(a)
        return 200, "ok"

    monkeypatch.setattr("coding_agent_notifier.sinks.slack.http_post_json", fake_post)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")
    rc = cli.main(["--config", str(cfg), "test", "--force", "--kind", "turn_complete"])
    assert rc == 0
    assert len(calls) == 1


def test_test_subcommand_suppressed(monkeypatch, tmp_path: Path, capsys):
    cfg = _write_config(
        tmp_path,
        """
gating = "idle_only"
idle_threshold_seconds = 9999
[sinks.slack]
enabled = true
webhook_url = "https://hook.test/x"
""".strip(),
    )
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")
    rc = cli.main(["--config", str(cfg), "test"])
    assert rc == 0
    assert "Gating suppressed" in capsys.readouterr().err


def test_doctor_runs(monkeypatch, tmp_path: Path, capsys):
    cfg = _write_config(tmp_path, "gating = \"always\"\n")
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 12.0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")
    rc = cli.main(["--config", str(cfg), "doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "gating: always" in out
    assert "idle=12.0s" in out


def test_doctor_reports_config_error(monkeypatch, tmp_path: Path, capsys):
    cfg = _write_config(tmp_path, 'gating = "nonsense"\n')
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: None)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: None)
    rc = cli.main(["--config", str(cfg), "doctor"])
    assert rc == 1
    assert "failed to load" in capsys.readouterr().out


def test_dispatch_swallows_sink_errors(monkeypatch, tmp_path: Path, capsys):
    cfg = _write_config(
        tmp_path,
        """
gating = "always"
[sinks.slack]
enabled = true
webhook_url = "https://hook.test/x"
""".strip(),
    )

    def fake_post(*a, **k):
        return 500, "nope"

    monkeypatch.setattr("coding_agent_notifier.sinks.slack.http_post_json", fake_post)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")
    payload = {"hook_event_name": "Stop", "cwd": "/tmp", "session_id": "s"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "slack sink failed" in err
