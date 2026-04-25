from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from coding_agent_notifier import pending_approvals as pa


def test_create_writes_record_and_fifo(tmp_path: Path):
    pa.create(
        "abc",
        agent="claude-code",
        session_id="sess-1",
        tool_name="Bash",
        tool_input={"command": "ls"},
        base_dir=tmp_path,
    )
    record = tmp_path / "abc.json"
    fifo = tmp_path / "abc.fifo"
    assert record.exists()
    assert fifo.exists()
    data = json.loads(record.read_text())
    assert data["agent"] == "claude-code"
    assert data["session_id"] == "sess-1"
    assert data["tool_name"] == "Bash"
    assert data["tool_input"] == {"command": "ls"}
    assert data["decision"] is None
    # Back-compat: records created without an explicit workspace are tagged
    # "default" so older records still read fine.
    assert data["workspace"] == "default"


def test_create_persists_workspace(tmp_path: Path):
    pa.create(
        "abc",
        agent="claude-code",
        session_id="s",
        tool_name="Bash",
        workspace="work",
        base_dir=tmp_path,
    )
    data = json.loads((tmp_path / "abc.json").read_text())
    assert data["workspace"] == "work"


def test_set_message_ref_updates_record(tmp_path: Path):
    pa.create("abc", agent="claude-code", session_id=None, tool_name=None, base_dir=tmp_path)
    pa.set_message_ref("abc", "C0123", "1735689600.001", base_dir=tmp_path)
    data = pa.read("abc", base_dir=tmp_path)
    assert data["channel"] == "C0123"
    assert data["message_ts"] == "1735689600.001"


def test_set_message_ref_on_missing_record_is_noop(tmp_path: Path):
    # Must not raise, must not create a new file.
    pa.set_message_ref("nope", "C0", "1.0", base_dir=tmp_path)
    assert pa.read("nope", base_dir=tmp_path) is None


def test_resolve_marks_decision_and_actor(tmp_path: Path):
    pa.create("abc", agent="claude-code", session_id=None, tool_name="Bash", base_dir=tmp_path)
    rec = pa.resolve("abc", "allow", actor="U123", base_dir=tmp_path)
    assert rec is not None
    assert rec["decision"] == "allow"
    assert rec["actor"] == "U123"
    on_disk = pa.read("abc", base_dir=tmp_path)
    assert on_disk["decision"] == "allow"
    assert on_disk["resolved_at"] is not None


def test_resolve_twice_is_idempotent(tmp_path: Path):
    pa.create("abc", agent="claude-code", session_id=None, tool_name=None, base_dir=tmp_path)
    first = pa.resolve("abc", "allow", actor="U1", base_dir=tmp_path)
    second = pa.resolve("abc", "deny", actor="U2", base_dir=tmp_path)
    assert first["decision"] == "allow"
    # Second call is a no-op: returns existing state, does not overwrite.
    assert second["decision"] == "allow"
    assert second["actor"] == "U1"


def test_resolve_missing_returns_none(tmp_path: Path):
    assert pa.resolve("nope", "allow", base_dir=tmp_path) is None


def test_resolve_invalid_decision_raises(tmp_path: Path):
    pa.create("abc", agent="claude-code", session_id=None, tool_name=None, base_dir=tmp_path)
    with pytest.raises(ValueError):
        pa.resolve("abc", "maybe", base_dir=tmp_path)  # type: ignore[arg-type]


def test_wait_returns_record_written_before_wait(tmp_path: Path):
    pa.create("abc", agent="claude-code", session_id=None, tool_name=None, base_dir=tmp_path)
    pa.resolve("abc", "deny", base_dir=tmp_path)
    # Already resolved before we even start waiting — wait returns the
    # full record immediately.
    rec = pa.wait("abc", timeout=0.5, base_dir=tmp_path)
    assert rec is not None
    assert rec["decision"] == "deny"


def test_wait_times_out_without_resolve(tmp_path: Path):
    pa.create("abc", agent="claude-code", session_id=None, tool_name=None, base_dir=tmp_path)
    assert pa.wait("abc", timeout=0.2, base_dir=tmp_path) is None


def test_wait_wakes_on_concurrent_resolve(tmp_path: Path):
    pa.create("abc", agent="claude-code", session_id=None, tool_name=None, base_dir=tmp_path)

    def resolver():
        time.sleep(0.1)
        pa.resolve("abc", "allow", actor="U9", base_dir=tmp_path)

    t = threading.Thread(target=resolver)
    t.start()
    try:
        # Generous timeout; actual resolve arrives ~100ms in.
        start = time.monotonic()
        got = pa.wait("abc", timeout=5.0, base_dir=tmp_path)
        elapsed = time.monotonic() - start
    finally:
        t.join()

    assert got is not None
    assert got["decision"] == "allow"
    assert got["actor"] == "U9"
    assert elapsed < 2.0, f"wait took too long: {elapsed:.2f}s — FIFO wake did not fire"


def test_resolve_with_selected_option_stores_index(tmp_path: Path):
    """When the user clicks an AskUserQuestion option button, the resolved
    record carries the index back so the waiting hook can pre-fill the
    answer via PermissionRequest's updatedInput."""
    pa.create(
        "abc",
        agent="claude-code",
        session_id=None,
        tool_name="AskUserQuestion",
        tool_input={"questions": [{"question": "Q?", "options": [{"label": "A"}, {"label": "B"}]}]},
        base_dir=tmp_path,
    )
    rec = pa.resolve("abc", "allow", actor="U1", selected_option=1, base_dir=tmp_path)
    assert rec is not None
    assert rec["decision"] == "allow"
    assert rec["selected_option_index"] == 1
    # Re-read via wait to confirm persistence.
    got = pa.wait("abc", timeout=0.5, base_dir=tmp_path)
    assert got is not None
    assert got["selected_option_index"] == 1


def test_resolve_without_selected_option_leaves_index_none(tmp_path: Path):
    pa.create("abc", agent="claude-code", session_id=None, tool_name="Bash", base_dir=tmp_path)
    rec = pa.resolve("abc", "allow", actor="U1", base_dir=tmp_path)
    assert rec is not None
    assert rec["selected_option_index"] is None


def test_cleanup_removes_files(tmp_path: Path):
    pa.create("abc", agent="claude-code", session_id=None, tool_name=None, base_dir=tmp_path)
    assert (tmp_path / "abc.json").exists()
    assert (tmp_path / "abc.fifo").exists()
    pa.cleanup("abc", base_dir=tmp_path)
    assert not (tmp_path / "abc.json").exists()
    assert not (tmp_path / "abc.fifo").exists()


def test_cleanup_missing_is_noop(tmp_path: Path):
    pa.cleanup("nope", base_dir=tmp_path)  # no error


def test_list_pending_skips_resolved(tmp_path: Path):
    pa.create("a", agent="claude-code", session_id=None, tool_name=None, base_dir=tmp_path)
    pa.create("b", agent="claude-code", session_id=None, tool_name=None, base_dir=tmp_path)
    pa.resolve("a", "allow", base_dir=tmp_path)
    pending = pa.list_pending(base_dir=tmp_path)
    ids = {rec["approval_id"] for rec in pending}
    assert ids == {"b"}


def test_list_pending_empty_dir(tmp_path: Path):
    assert pa.list_pending(base_dir=tmp_path / "does-not-exist") == []


def test_list_pending_skips_corrupt_records(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "ok.json").write_text(json.dumps({
        "approval_id": "ok", "decision": None, "created_at": time.time()
    }))
    pending = pa.list_pending(base_dir=tmp_path)
    assert [r["approval_id"] for r in pending] == ["ok"]


def test_gc_stale_removes_old_records(tmp_path: Path):
    now = [1_000.0]
    pa.create("old", agent="claude-code", session_id=None, tool_name=None,
              base_dir=tmp_path, clock=lambda: now[0])
    now[0] = 1_000.0 + 7200  # 2h later
    pa.create("new", agent="claude-code", session_id=None, tool_name=None,
              base_dir=tmp_path, clock=lambda: now[0])
    removed = pa.gc_stale(max_age_seconds=3600, base_dir=tmp_path, clock=lambda: now[0])
    assert removed == 1
    assert not (tmp_path / "old.json").exists()
    assert (tmp_path / "new.json").exists()


def test_gc_stale_empty_dir(tmp_path: Path):
    assert pa.gc_stale(base_dir=tmp_path / "nope") == 0


def test_default_approvals_dir_uses_dot_dir(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    d = pa.default_approvals_dir()
    assert str(d).startswith(str(tmp_path))
    assert "approvals" in d.parts


def test_default_approvals_dir_fallback(monkeypatch):
    monkeypatch.delenv("AGENT_NOTIFY_HOME", raising=False)
    d = pa.default_approvals_dir()
    assert ".agent-notify" in d.parts


def test_safe_filenames_sanitize_path_chars(tmp_path: Path):
    # Ensure nothing in the id escapes the base dir, even if it contains /
    pa.create("weird/../id", agent="claude-code", session_id=None, tool_name=None,
              base_dir=tmp_path)
    matches = list(tmp_path.glob("*.json"))
    assert len(matches) == 1
    # Every non-alnum/dash/underscore got sanitized to _
    assert "/" not in matches[0].name
