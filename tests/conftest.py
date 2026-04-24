from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    def _load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text())
    return _load


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path_factory, monkeypatch):
    """Keep dedup writes off the user's real cache. Individual tests may still
    override XDG_CACHE_HOME / monkeypatch default_state_path for specifics."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))


@pytest.fixture(autouse=True)
def _inline_defer_child(monkeypatch):
    """Run turn_complete's deferred dispatch inline.

    In production the CLI spawns a detached subprocess that sleeps for the
    coalesce window before dispatching. Forking in tests would break assertions
    that inspect `calls` after the hook returns, so we substitute an in-process
    call that also skips the sleep. Tests that want to exercise the defer path
    directly (test_coalesce.py) override this again locally.
    """
    from coding_agent_notifier import cli

    def _inline_spawn(config_path, agent, session_id):
        argv: list[str] = []
        if config_path is not None:
            argv += ["--config", str(config_path)]
        argv += ["_defer-dispatch", agent, session_id or ""]
        cli.main(argv)

    monkeypatch.setattr(cli, "_spawn_defer_child", _inline_spawn)
    monkeypatch.setattr(cli, "_sleep", lambda _s: None)
