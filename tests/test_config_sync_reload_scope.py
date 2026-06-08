"""Reload-scope tests: casa_reload(scope='config_sync')."""
from __future__ import annotations

import types
from pathlib import Path

import pytest

import reload as reload_mod

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(tmp_path: Path):
    cfg = tmp_path / "config"
    (cfg / "agents").mkdir(parents=True)
    (cfg / "policies").mkdir(parents=True)
    rt = types.SimpleNamespace()
    rt.config_dir = str(cfg)
    rt.defaults_dir = str(tmp_path / "defaults")
    rt.data_dir = str(tmp_path / "data")
    (Path(rt.defaults_dir) / "agents").mkdir(parents=True)
    (Path(rt.defaults_dir) / "policies").mkdir(parents=True)
    return rt


async def test_config_sync_scope_registered() -> None:
    assert "config_sync" in reload_mod._HANDLERS


async def test_config_sync_handler_runs_reconcile_and_chains(monkeypatch, runtime) -> None:
    calls: list[str] = []

    def fake_run(**kwargs):
        calls.append("reconcile")
        return 0
    monkeypatch.setattr("config_sync.run", fake_run)

    async def fake_agents(rt, *, role=None):
        calls.append("agents")
        return ["reloaded_agents"]

    async def fake_policies(rt, *, role=None):
        calls.append("policies")
        return ["reloaded_policies"]
    monkeypatch.setitem(reload_mod._HANDLERS, "agents", fake_agents)
    monkeypatch.setitem(reload_mod._HANDLERS, "policies", fake_policies)

    actions = await reload_mod.reload_config_sync(runtime, role=None)
    assert calls == ["reconcile", "agents", "policies"]
    assert any("reconcile" in a for a in actions)
