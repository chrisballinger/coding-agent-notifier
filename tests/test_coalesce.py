from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from coding_agent_notifier import cli, pending


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


@pytest.fixture
def fake_slack(monkeypatch):
    calls: list[tuple] = []

    def _post(url, body, headers=None, timeout=10.0):
        calls.append((url, body, headers))
        return 200, "ok"

    monkeypatch.setattr("coding_agent_notifier.sinks.slack.http_post_json", _post)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")
    return calls


def _basic_cfg(tmp_path: Path) -> Path:
    return _write_config(
        tmp_path,
        """
gating = "always"
[display]
verbosity = "terse"
coalesce_window_seconds = 2.5
[summary]
enabled = false
[sinks.slack]
enabled = true
webhook_url = "https://hook.test/x"
""".strip(),
    )


def test_turn_complete_dispatches_via_defer_child(monkeypatch, tmp_path: Path, fake_slack):
    cfg = _basic_cfg(tmp_path)
    payload = {"hook_event_name": "Stop", "cwd": "/tmp", "session_id": "s-ABC"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])
    # Conftest inlines the defer child — so exactly one Slack call happened.
    assert len(fake_slack) == 1


def test_idle_prompt_cancels_queued_turn_complete(monkeypatch, tmp_path: Path):
    """The core coalesce contract: if an idle_prompt follows turn_complete for
    the same session before the defer window fires, turn_complete is suppressed
    and only the idle_prompt pings."""
    cfg = _basic_cfg(tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json",
        lambda url, body, headers=None, timeout=10.0: (calls.append((url, body)) or (200, "ok")),
    )
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")

    # Override the inlined defer from conftest: defer must NOT run immediately
    # here, otherwise we can't simulate the race. We replace it with a recorder
    # and invoke manually after idle_prompt has claimed the pending entry.
    recorded: list[tuple] = []
    monkeypatch.setattr(
        cli, "_spawn_defer_child",
        lambda cfg_path, agent, sid: recorded.append((cfg_path, agent, sid)),
    )
    monkeypatch.setattr(cli, "_sleep", lambda _s: None)

    # Fire turn_complete. This should write a pending file + "spawn" (record).
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"hook_event_name": "Stop", "cwd": "/tmp", "session_id": "sess-1"})
    ))
    cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])
    assert recorded == [(Path(str(cfg)), "claude-code", "sess-1")]
    assert calls == []  # not dispatched yet

    # Now fire idle_prompt for the same session. It should claim the pending
    # entry (invalidating the turn_complete) AND dispatch itself.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "Notification",
        "notification_type": "idle_prompt",
        "cwd": "/tmp",
        "session_id": "sess-1",
        "message": "still here?",
    })))
    cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])
    assert len(calls) == 1  # only idle_prompt

    # Now run the defer child manually. Pending file was already claimed → no
    # additional dispatch.
    cli.main(["--config", str(cfg), "_defer-dispatch", "claude-code", "sess-1"])
    assert len(calls) == 1


def test_defer_child_dispatches_when_no_idle_prompt_arrives(monkeypatch, tmp_path: Path):
    """When nothing claims the pending file, the defer child sends the turn_complete."""
    cfg = _basic_cfg(tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json",
        lambda url, body, headers=None, timeout=10.0: (calls.append((url, body)) or (200, "ok")),
    )
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")
    monkeypatch.setattr(cli, "_spawn_defer_child", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_sleep", lambda _s: None)

    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"hook_event_name": "Stop", "cwd": "/tmp", "session_id": "sess-x"})
    ))
    cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])
    assert calls == []

    cli.main(["--config", str(cfg), "_defer-dispatch", "claude-code", "sess-x"])
    assert len(calls) == 1


def test_defer_child_silent_when_pending_absent(monkeypatch, tmp_path: Path):
    """If no pending entry exists (e.g. cleaned up already), the child exits quietly."""
    cfg = _basic_cfg(tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json",
        lambda *a, **k: (calls.append(a) or (200, "ok")),
    )
    monkeypatch.setattr(cli, "_sleep", lambda _s: None)
    cli.main(["--config", str(cfg), "_defer-dispatch", "claude-code", "nope"])
    assert calls == []


def test_defer_child_applies_transcript_snippet(monkeypatch, tmp_path: Path):
    """When summary is enabled and an event carries a transcript_path, the
    child reads the last assistant text and folds it into event.message."""
    transcript_file = tmp_path / "t.jsonl"
    transcript_file.write_text(json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Here is what I did: changed X and Y."}],
        },
    }))

    cfg = _write_config(
        tmp_path,
        f"""
gating = "always"
[display]
coalesce_window_seconds = 0.01
[summary]
enabled = true
head_chars = 50
tail_chars = 50
[sinks.slack]
enabled = true
webhook_url = "https://hook.test/x"
""".strip(),
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json",
        lambda url, body, headers=None, timeout=10.0: (calls.append((url, body)) or (200, "ok")),
    )
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")
    monkeypatch.setattr(cli, "_spawn_defer_child", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_sleep", lambda _s: None)

    payload = {
        "hook_event_name": "Stop",
        "cwd": "/tmp",
        "session_id": "sess-snip",
        "transcript_path": str(transcript_file),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])

    cli.main(["--config", str(cfg), "_defer-dispatch", "claude-code", "sess-snip"])
    assert len(calls) == 1
    body = json.dumps(calls[0][1])
    assert "Here is what I did" in body


def test_idle_prompt_picks_up_transcript_snippet(monkeypatch, tmp_path: Path):
    """The main win: when turn_complete is coalesced away by a follow-up
    idle_prompt, the snippet must attach to the idle_prompt instead — otherwise
    the user loses the 'what did the agent just do' context entirely."""
    transcript_file = tmp_path / "t.jsonl"
    transcript_file.write_text(json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "I changed foo.py and bar.py."}],
        },
    }))
    cfg = _write_config(
        tmp_path,
        """
gating = "always"
[display]
coalesce_window_seconds = 2.5
[summary]
enabled = true
head_chars = 250
tail_chars = 250
[sinks.slack]
enabled = true
webhook_url = "https://hook.test/x"
""".strip(),
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json",
        lambda url, body, headers=None, timeout=10.0: (calls.append((url, body)) or (200, "ok")),
    )
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")

    payload = {
        "hook_event_name": "Notification",
        "notification_type": "idle_prompt",
        "cwd": "/tmp",
        "session_id": "sess-idle",
        "message": "Claude is waiting for your input",
        "transcript_path": str(transcript_file),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])
    assert len(calls) == 1
    body_json = json.dumps(calls[0][1])
    assert "I changed foo.py and bar.py." in body_json
    # Original message is replaced — no "waiting for your input" in body.
    assert "waiting for your input" not in body_json


def test_idle_prompt_without_transcript_keeps_original_message(monkeypatch, tmp_path: Path):
    cfg = _basic_cfg(tmp_path)  # summary.enabled = false in _basic_cfg
    calls: list[tuple] = []
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json",
        lambda url, body, headers=None, timeout=10.0: (calls.append((url, body)) or (200, "ok")),
    )
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")

    payload = {
        "hook_event_name": "Notification",
        "notification_type": "idle_prompt",
        "cwd": "/tmp",
        "session_id": "sess-1",
        "message": "Claude is waiting for your input",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])
    body_json = json.dumps(calls[0][1])
    assert "waiting for your input" in body_json


def test_force_flag_bypasses_defer(monkeypatch, tmp_path: Path):
    """--force skips the defer path: the notification goes out immediately."""
    cfg = _basic_cfg(tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json",
        lambda *a, **k: (calls.append(a) or (200, "ok")),
    )
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")
    # Even though _spawn_defer_child would run inline, --force takes the
    # non-defer path entirely.
    recorded: list[tuple] = []
    monkeypatch.setattr(
        cli, "_spawn_defer_child",
        lambda cfg_path, agent, sid: recorded.append((cfg_path, agent, sid)),
    )

    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"hook_event_name": "Stop", "cwd": "/tmp", "session_id": "sess-f"})
    ))
    cli.main(["--config", str(cfg), "hook", "--source", "claude-code", "--force"])
    assert recorded == []
    assert len(calls) == 1


def test_coalesce_disabled_when_window_zero(monkeypatch, tmp_path: Path):
    """With coalesce_window_seconds = 0, turn_complete dispatches inline."""
    cfg = _write_config(
        tmp_path,
        """
gating = "always"
[display]
coalesce_window_seconds = 0
[summary]
enabled = false
[sinks.slack]
enabled = true
webhook_url = "https://hook.test/x"
""".strip(),
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json",
        lambda *a, **k: (calls.append(a) or (200, "ok")),
    )
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")
    recorded: list[tuple] = []
    monkeypatch.setattr(
        cli, "_spawn_defer_child",
        lambda cfg_path, agent, sid: recorded.append((cfg_path, agent, sid)),
    )

    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"hook_event_name": "Stop", "cwd": "/tmp", "session_id": "sess-z"})
    ))
    cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])
    assert recorded == []
    assert len(calls) == 1


def test_gating_still_applies_before_defer(monkeypatch, tmp_path: Path):
    """If gating would suppress, we don't fork a defer child."""
    cfg = _write_config(
        tmp_path,
        """
gating = "idle_only"
idle_threshold_seconds = 9999
[display]
coalesce_window_seconds = 2.5
[sinks.slack]
enabled = true
webhook_url = "https://hook.test/x"
""".strip(),
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        "coding_agent_notifier.sinks.slack.http_post_json",
        lambda *a, **k: (calls.append(a) or (200, "ok")),
    )
    monkeypatch.setattr("coding_agent_notifier.cli.macos.idle_seconds", lambda: 0)
    monkeypatch.setattr("coding_agent_notifier.cli.macos.frontmost_app", lambda: "iTerm2")
    recorded: list[tuple] = []
    monkeypatch.setattr(
        cli, "_spawn_defer_child",
        lambda cfg_path, agent, sid: recorded.append((cfg_path, agent, sid)),
    )

    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"hook_event_name": "Stop", "cwd": "/tmp", "session_id": "s"})
    ))
    cli.main(["--config", str(cfg), "hook", "--source", "claude-code"])
    assert recorded == []
    assert calls == []


def test_pending_write_uses_real_cache_dir(monkeypatch, tmp_path: Path):
    """Production spawn path writes to XDG_CACHE_HOME; conftest already
    isolates that so there's nothing for tests to do beyond sanity-checking."""
    path = pending._path_for("claude-code", "sess-1")
    # XDG_CACHE_HOME is redirected to a tmp dir by the autouse fixture, so
    # paths live under it (not the real ~/.cache).
    assert "pending" in str(path)