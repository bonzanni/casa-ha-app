"""§3.8: an engagement's plugin_artifacts binding is immutable and survives
every serialization layer (tombstone write + reload), and the run script
renders --plugin-dir flags from the RECORDED paths."""
from __future__ import annotations

import time

import pytest

from engagement_registry import EngagementRegistry

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

_ARTIFACTS = [
    {"name": "superpowers", "artifact_id": "a" * 64,
     "path": "/config/plugins/store/superpowers/" + "a" * 64},
    {"name": "context7", "artifact_id": "b" * 64,
     "path": "/config/plugins/store/context7/" + "b" * 64},
]


async def test_create_persists_plugin_artifacts_roundtrip(tmp_path):
    path = str(tmp_path / "e.json")
    reg = EngagementRegistry(tombstone_path=path, bus=None)
    rec = await reg.create(
        "executor", "plugin-developer", "claude_code", "task", {}, 1,
        plugin_artifacts=_ARTIFACTS)
    assert rec.plugin_artifacts == tuple(_ARTIFACTS)
    # Reload from disk — the field survives serialization.
    reg2 = EngagementRegistry(tombstone_path=path, bus=None)
    await reg2.load()
    reloaded = reg2.get(rec.id)
    assert reloaded is not None
    assert list(reloaded.plugin_artifacts) == _ARTIFACTS


async def test_every_mutator_preserves_plugin_artifacts(tmp_path):
    path = str(tmp_path / "e.json")
    reg = EngagementRegistry(tombstone_path=path, bus=None)
    rec = await reg.create(
        "executor", "plugin-developer", "claude_code", "task", {}, 1,
        plugin_artifacts=_ARTIFACTS)
    await reg.mark_idle(rec.id)
    await reg.persist_session_id(rec.id, "sess-xyz")
    await reg.mark_completed(rec.id, completed_at=time.time())
    reg2 = EngagementRegistry(tombstone_path=path, bus=None)
    await reg2.load()
    reloaded = reg2.get(rec.id)
    assert list(reloaded.plugin_artifacts) == _ARTIFACTS


async def test_run_script_contains_plugin_dir_flags_from_record(tmp_path):
    from types import SimpleNamespace
    from drivers.workspace import render_run_script
    eng = SimpleNamespace(id="e" * 32, plugin_artifacts=_ARTIFACTS)
    out = render_run_script(
        engagement_id=eng.id, permission_mode="acceptEdits", extra_dirs=[],
        plugin_dirs=[pa["path"] for pa in eng.plugin_artifacts])
    for pa in _ARTIFACTS:
        assert f"--plugin-dir {pa['path']}" in out
