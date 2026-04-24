from __future__ import annotations

from pathlib import Path

from coding_agent_notifier import pending
from coding_agent_notifier.event import Event


def _event(**kw) -> Event:
    base = dict(
        agent="claude-code",
        kind="turn_complete",
        message="hello",
        cwd=Path("/tmp/x"),
        session_id="sess-1",
    )
    base.update(kw)
    return Event(**base)


def test_write_then_claim_round_trips(tmp_path: Path):
    e = _event(tool_input={"k": "v"}, source_app="iTerm2")
    pending.write(e, base_dir=tmp_path)
    claimed = pending.claim("claude-code", "sess-1", base_dir=tmp_path)
    assert claimed is not None
    assert claimed.agent == "claude-code"
    assert claimed.message == "hello"
    assert claimed.cwd == Path("/tmp/x")
    assert claimed.tool_input == {"k": "v"}
    assert claimed.source_app == "iTerm2"


def test_claim_returns_none_when_absent(tmp_path: Path):
    assert pending.claim("claude-code", "never", base_dir=tmp_path) is None


def test_claim_removes_file(tmp_path: Path):
    pending.write(_event(), base_dir=tmp_path)
    pending.claim("claude-code", "sess-1", base_dir=tmp_path)
    # Second claim should be None (file was deleted).
    assert pending.claim("claude-code", "sess-1", base_dir=tmp_path) is None


def test_claim_rejects_stale_entries(tmp_path: Path):
    current = [1000.0]
    pending.write(_event(), base_dir=tmp_path, clock=lambda: current[0])
    # Advance past max_age_seconds
    current[0] += 999
    assert pending.claim(
        "claude-code", "sess-1", base_dir=tmp_path,
        max_age_seconds=60, clock=lambda: current[0],
    ) is None


def test_overwrites_previous_entry(tmp_path: Path):
    pending.write(_event(message="first"), base_dir=tmp_path)
    pending.write(_event(message="second"), base_dir=tmp_path)
    claimed = pending.claim("claude-code", "sess-1", base_dir=tmp_path)
    assert claimed is not None
    assert claimed.message == "second"


def test_per_session_isolation(tmp_path: Path):
    pending.write(_event(session_id="a"), base_dir=tmp_path)
    pending.write(_event(session_id="b"), base_dir=tmp_path)
    a = pending.claim("claude-code", "a", base_dir=tmp_path)
    b = pending.claim("claude-code", "b", base_dir=tmp_path)
    assert a is not None and b is not None


def test_per_agent_isolation(tmp_path: Path):
    pending.write(_event(agent="claude-code", session_id="s"), base_dir=tmp_path)
    pending.write(_event(agent="codex", session_id="s"), base_dir=tmp_path)
    assert pending.claim("claude-code", "s", base_dir=tmp_path) is not None
    assert pending.claim("codex", "s", base_dir=tmp_path) is not None


def test_session_id_none_allowed(tmp_path: Path):
    pending.write(_event(session_id=None), base_dir=tmp_path)
    claimed = pending.claim("claude-code", None, base_dir=tmp_path)
    assert claimed is not None
    assert claimed.session_id is None


def test_sanitizes_weird_session_ids(tmp_path: Path):
    # Should not crash on odd chars, and should be reclaimable with the same value.
    pending.write(_event(session_id="a/b/c"), base_dir=tmp_path)
    claimed = pending.claim("claude-code", "a/b/c", base_dir=tmp_path)
    assert claimed is not None


def test_transcript_path_round_trips(tmp_path: Path):
    e = _event(transcript_path=Path("/var/log/tx.jsonl"))
    pending.write(e, base_dir=tmp_path)
    claimed = pending.claim("claude-code", "sess-1", base_dir=tmp_path)
    assert claimed is not None
    assert claimed.transcript_path == Path("/var/log/tx.jsonl")


def test_corrupt_file_yields_none(tmp_path: Path):
    path = pending._path_for("claude-code", "s1", base_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    assert pending.claim("claude-code", "s1", base_dir=tmp_path) is None


def test_default_pending_dir_honors_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    d = pending.default_pending_dir()
    assert str(d).startswith(str(tmp_path))
