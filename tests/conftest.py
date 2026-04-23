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
