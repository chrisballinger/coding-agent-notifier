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
def _isolate_state(tmp_path_factory, monkeypatch):
    """Keep every dedup / pending / approvals / log write off the user's real
    dot dir. Individual tests may override AGENT_NOTIFY_HOME for specifics.

    Also neuters legacy-migration lookups: we don't want tests reading
    (or worse, moving) files from the developer's real ~/.config or
    ~/.cache. Point them at a throwaway tmp location that doesn't exist.
    """
    from coding_agent_notifier import paths
    fresh = tmp_path_factory.mktemp("agent-notify-home")
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(fresh))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # Redirect legacy-migration lookups to non-existent paths so
    # `migrate_legacy_state` is a no-op during tests that don't
    # exercise it directly (test_migration.py overrides this again).
    stub_legacy = fresh / "_legacy-not-there"
    monkeypatch.setattr(paths, "_LEGACY_CONFIG_DIR", stub_legacy / "config")
    monkeypatch.setattr(paths, "_LEGACY_CACHE_DIR", stub_legacy / "cache")


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
