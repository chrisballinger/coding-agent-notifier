from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent_notifier import expandable_messages


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    d = tmp_path / "expandable_messages"
    d.mkdir()
    return d


def _record(**overrides):
    base = dict(
        message_id="msg-1",
        workspace="default",
        channel="D0123",
        message_ts="1700000000.000100",
        preview_body={"text": "preview", "attachments": [{"blocks": []}]},
        full_body={"text": "full", "attachments": [{"blocks": []}]},
    )
    base.update(overrides)
    return base


def test_create_writes_record_and_read_round_trips(store_dir):
    expandable_messages.create(base_dir=store_dir, **_record())
    out = expandable_messages.read("msg-1", base_dir=store_dir)
    assert out is not None
    assert out["message_id"] == "msg-1"
    assert out["channel"] == "D0123"
    assert out["preview_body"]["text"] == "preview"
    assert out["full_body"]["text"] == "full"
    assert isinstance(out["created_at"], float)


def test_read_returns_none_for_missing_record(store_dir):
    assert expandable_messages.read("does-not-exist", base_dir=store_dir) is None


def test_cleanup_removes_record(store_dir):
    expandable_messages.create(base_dir=store_dir, **_record())
    assert expandable_messages.read("msg-1", base_dir=store_dir) is not None
    expandable_messages.cleanup("msg-1", base_dir=store_dir)
    assert expandable_messages.read("msg-1", base_dir=store_dir) is None


def test_cleanup_missing_record_is_noop(store_dir):
    expandable_messages.cleanup("never-existed", base_dir=store_dir)  # no raise


def test_safe_id_sanitizes_filename(store_dir):
    # Slashes / weird chars must not escape the store dir.
    expandable_messages.create(
        base_dir=store_dir,
        **_record(message_id="../naughty/id with spaces"),
    )
    files = list(store_dir.glob("*.json"))
    assert len(files) == 1
    assert "/" not in files[0].name
    out = expandable_messages.read("../naughty/id with spaces", base_dir=store_dir)
    assert out is not None


def test_gc_stale_removes_old_records_and_keeps_fresh(store_dir):
    fixed_now = 1_000_000.0
    expandable_messages.create(
        base_dir=store_dir,
        clock=lambda: fixed_now - 10_000,  # ~2.7h old
        **_record(message_id="old"),
    )
    expandable_messages.create(
        base_dir=store_dir,
        clock=lambda: fixed_now - 100,  # 100s old
        **_record(message_id="fresh"),
    )
    removed = expandable_messages.gc_stale(
        base_dir=store_dir,
        max_age_seconds=3600.0,
        clock=lambda: fixed_now,
    )
    assert removed == 1
    assert expandable_messages.read("old", base_dir=store_dir) is None
    assert expandable_messages.read("fresh", base_dir=store_dir) is not None


def test_gc_stale_on_empty_dir_returns_zero(store_dir):
    assert expandable_messages.gc_stale(base_dir=store_dir) == 0


def test_gc_stale_on_missing_dir_returns_zero(tmp_path):
    nonexistent = tmp_path / "does-not-exist"
    assert expandable_messages.gc_stale(base_dir=nonexistent) == 0
