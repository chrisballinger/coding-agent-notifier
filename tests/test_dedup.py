from __future__ import annotations

from pathlib import Path

from coding_agent_notifier import dedup


class _FakeClock:
    def __init__(self, start: float = 1_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_first_call_not_seen(tmp_path: Path):
    path = tmp_path / "d.json"
    assert dedup.recently_seen("k", ttl=5, path=path, clock=_FakeClock()) is False


def test_second_call_within_ttl_is_duplicate(tmp_path: Path):
    path = tmp_path / "d.json"
    clock = _FakeClock()
    dedup.recently_seen("k", ttl=5, path=path, clock=clock)
    clock.advance(1)
    assert dedup.recently_seen("k", ttl=5, path=path, clock=clock) is True


def test_call_after_ttl_is_fresh(tmp_path: Path):
    path = tmp_path / "d.json"
    clock = _FakeClock()
    dedup.recently_seen("k", ttl=5, path=path, clock=clock)
    clock.advance(10)
    assert dedup.recently_seen("k", ttl=5, path=path, clock=clock) is False


def test_distinct_keys_do_not_interfere(tmp_path: Path):
    path = tmp_path / "d.json"
    clock = _FakeClock()
    assert dedup.recently_seen("a", ttl=5, path=path, clock=clock) is False
    assert dedup.recently_seen("b", ttl=5, path=path, clock=clock) is False
    assert dedup.recently_seen("a", ttl=5, path=path, clock=clock) is True


def test_expired_entries_are_evicted(tmp_path: Path):
    path = tmp_path / "d.json"
    clock = _FakeClock()
    dedup.recently_seen("a", ttl=5, path=path, clock=clock)
    clock.advance(10)
    dedup.recently_seen("b", ttl=5, path=path, clock=clock)
    import json
    data = json.loads(path.read_text())
    assert "a" not in data
    assert "b" in data


def test_tolerates_corrupt_state_file(tmp_path: Path):
    path = tmp_path / "d.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert dedup.recently_seen("k", ttl=5, path=path, clock=_FakeClock()) is False


def test_tolerates_non_dict_state(tmp_path: Path):
    path = tmp_path / "d.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1,2,3]")
    assert dedup.recently_seen("k", ttl=5, path=path, clock=_FakeClock()) is False


def test_default_state_path_uses_xdg(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert str(dedup.default_state_path()).startswith(str(tmp_path))


def test_default_state_path_fallback(monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert ".cache" in str(dedup.default_state_path())


def test_dedup_key_shape():
    assert dedup.dedup_key("claude-code", "permission", "abc", "Bash") == "claude-code:permission:abc:Bash"
    assert dedup.dedup_key("codex", "permission", None, None) == "codex:permission::"
