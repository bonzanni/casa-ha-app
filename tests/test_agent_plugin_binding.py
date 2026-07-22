"""§3.3/§3.9: agents consume the resolver snapshot; active binding recorded
at construction; resolve happens off-loop; specialist options resolve with
their concrete role; executor options carry plugins but no grants/callback."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import plugin_registry
from plugin_registry import ResolutionResult, reload_snapshot
import agent as agent_mod
from agent import Agent
from channels import ChannelManager
from config import (
    AgentConfig, CharacterConfig, HooksConfig, MemoryConfig, ToolsConfig,
)
from mcp_registry import McpServerRegistry
from session_registry import SessionRegistry
from plugin_fixtures import entry, mk_artifact, mk_registry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.unit


def _make_agent(tmp_path, role="assistant", agent_registry=None) -> Agent:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, 
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
    )
    sm = AsyncMock()
    sm.profile.return_value = ""
    sm.recall.return_value = ""
    return Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        agent_registry=agent_registry,
        semantic_memory=sm,
    )


def test_resident_options_use_resolver_and_record_binding(tmp_path):
    store = tmp_path / "store"
    e = entry("probe", ["resident:assistant"])
    art = mk_artifact(store, "probe", e["artifact_id"],
                      mcp_servers={"probe": {}})
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    a = _make_agent(tmp_path, role="assistant")

    async def run():
        opts = await a._build_options(
            channel="telegram", channel_key="k", is_fresh=True,
            resume_sid=None, user_text="hi")
        assert opts.plugins == [{"type": "local", "path": str(art)}]
        assert "mcp__plugin_probe_probe" in opts.allowed_tools
        assert a.active_plugin_binding == {"probe": e["artifact_id"]}

    asyncio.run(run())


def test_resolution_cached_per_instance_even_when_empty(tmp_path, monkeypatch):
    reload_snapshot(registry_path=tmp_path / "absent.json",
                    store_root=tmp_path / "store")
    calls = {"n": 0}
    real = plugin_registry.resolve_for

    def counting(target):
        calls["n"] += 1
        return real(target)

    monkeypatch.setattr(plugin_registry, "resolve_for", counting)
    a = _make_agent(tmp_path)

    async def run():
        r1 = await a._get_plugin_resolution()
        r2 = await a._get_plugin_resolution()
        assert r1 is r2                       # cached even when empty
        assert r1.plugins == []
        assert calls["n"] == 1                # resolved exactly once

    asyncio.run(run())


def test_resolve_runs_off_loop(tmp_path, monkeypatch):
    def slow(target):
        time.sleep(0.3)
        return ResolutionResult(registry_valid=True)

    monkeypatch.setattr(plugin_registry, "resolve_for", slow)
    a = _make_agent(tmp_path)

    async def run():
        ticks = 0

        async def tick():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        t = asyncio.create_task(tick())
        await asyncio.sleep(0)
        await a._get_plugin_resolution()
        t.cancel()
        assert ticks >= 10, f"event loop starved during resolve (ticks={ticks})"

    asyncio.run(run())


def _spec_cfg(role="finance", cwd=""):
    return SimpleNamespace(
        role=role, model="claude-sonnet-4-6", system_prompt="You are Alex.",
        tools=SimpleNamespace(allowed=["Read", "Skill"], disallowed=["Bash"],
                              permission_mode="acceptEdits", max_turns=10),
        mcp_server_names=[], hooks=HooksConfig(), cwd=cwd,
    )


def test_specialist_options_resolve_with_role(tmp_path, monkeypatch):
    import tools as tools_mod
    store = tmp_path / "store"
    e = entry("finplug", ["specialist:finance"])
    art = mk_artifact(store, "finplug", e["artifact_id"],
                      mcp_servers={"finplug": {}})
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    opts = tools_mod._build_specialist_options(_spec_cfg("finance"))
    assert opts.plugins == [{"type": "local", "path": str(art)}]
    assert "mcp__plugin_finplug_finplug" in opts.allowed_tools
    assert opts.can_use_tool is not None          # fail-closed callback


def test_specialist_project_scope_no_longer_dropped(tmp_path, monkeypatch):
    """The old role-less build_sdk_plugins dropped project-scope plugins for
    specialists; resolving with the concrete role fixes it (§3.3)."""
    import tools as tools_mod
    store = tmp_path / "store"
    e = entry("finplug", ["specialist:finance"])
    mk_artifact(store, "finplug", e["artifact_id"])
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    opts = tools_mod._build_specialist_options(_spec_cfg("finance"))
    assert len(opts.plugins) == 1                 # NOT dropped


def _exec_defn():
    return SimpleNamespace(
        hooks_path=None, mcp_server_names=[], tools_allowed=["Read"],
        model="claude-sonnet-4-6", permission_mode="auto", max_turns=None,
        tools_disallowed=[], driver="claude_code",
    )


def test_executor_options_have_plugins_but_no_grants_no_callback(tmp_path,
                                                                 monkeypatch):
    import tools as tools_mod
    store = tmp_path / "store"
    e = entry("execplug", ["executor:probe-exec"])
    art = mk_artifact(store, "execplug", e["artifact_id"],
                      mcp_servers={"execplug": {}})
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    opts = tools_mod._build_executor_options(_exec_defn(),
                                             executor_type="probe-exec")
    assert opts.plugins == [{"type": "local", "path": str(art)}]
    assert opts.can_use_tool is None              # executors keep the relay
    assert not any(t.startswith("mcp__plugin_") for t in opts.allowed_tools)


def test_executor_options_prefer_passed_resolution_over_fresh_resolve(
        tmp_path, monkeypatch):
    """Sol F5: one resolve feeds gate + record + options — a passed resolution
    is used verbatim and resolve_for is NOT called again."""
    import tools as tools_mod
    rp = SimpleNamespace(name="a", artifact_id="0" * 64, path="/store/a",
                         version="1.0.0", manifest={})
    passed = ResolutionResult(registry_valid=True, plugins=[rp])

    def boom(target):
        raise AssertionError("resolve_for must NOT be called")

    monkeypatch.setattr(plugin_registry, "resolve_for", boom)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    opts = tools_mod._build_executor_options(
        _exec_defn(), executor_type="x", resolution=passed)
    assert opts.plugins == [{"type": "local", "path": "/store/a"}]


def test_executor_resume_uses_recorded_paths_never_resolves(tmp_path,
                                                            monkeypatch):
    import tools as tools_mod
    from unittest.mock import MagicMock
    art_dir = tmp_path / "store" / "rec" / ("a" * 64)
    art_dir.mkdir(parents=True)

    def boom(target):
        raise AssertionError("resume must NOT re-resolve current assignments")

    monkeypatch.setattr(plugin_registry, "resolve_for", boom)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    exec_reg = MagicMock()
    exec_reg.get = MagicMock(return_value=_exec_defn())
    tools_mod.init_tools(
        channel_manager=MagicMock(), bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=None,
        trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        executor_registry=exec_reg,
    )
    eng = SimpleNamespace(
        kind="executor", role_or_type="rec",
        plugin_artifacts=[{"name": "rec", "artifact_id": "a" * 64,
                           "path": str(art_dir)}])
    opts = tools_mod.build_engagement_resume_options(eng, "sess-1")
    assert opts.plugins == [{"type": "local", "path": str(art_dir)}]

    # A missing recorded path fails the resume CLOSED.
    eng_missing = SimpleNamespace(
        kind="executor", role_or_type="rec",
        plugin_artifacts=[{"name": "rec", "artifact_id": "a" * 64,
                           "path": str(tmp_path / "gone")}])
    with pytest.raises(Exception):
        tools_mod.build_engagement_resume_options(eng_missing, "sess-2")


# --- §3.10 first-contact notice --------------------------------------------

def test_first_contact_prepends_once_then_consumed(tmp_path, monkeypatch):
    import plugin_health
    monkeypatch.setattr(plugin_health, "first_contact_notice",
                        lambda role: "PLUGIN-DEGRADED x (corrupt_artifact)")
    a = _make_agent(tmp_path)

    async def run():
        out = await a._maybe_prepend_health_notice("hello")
        assert out.startswith("PLUGIN-DEGRADED")
        assert out.endswith("hello")
        assert a._health_notice_pending is False
        out2 = await a._maybe_prepend_health_notice("again")
        assert out2 == "again"          # flag consumed → unprefixed

    asyncio.run(run())


def test_first_contact_healthy_turn_leaves_flag_pending(tmp_path, monkeypatch):
    """Sol F6: a healthy first turn must NOT burn the flag — a later-appearing
    issue still gets first-contact delivery."""
    import plugin_health
    state = {"notice": None}
    monkeypatch.setattr(plugin_health, "first_contact_notice",
                        lambda role: state["notice"])
    a = _make_agent(tmp_path)

    async def run():
        out = await a._maybe_prepend_health_notice("healthy")
        assert out == "healthy"
        assert a._health_notice_pending is True          # NOT consumed
        state["notice"] = "PLUGIN-DEGRADED y (reload_required)"
        out2 = await a._maybe_prepend_health_notice("now")
        assert out2.startswith("PLUGIN-DEGRADED")
        assert a._health_notice_pending is False

    asyncio.run(run())


def test_delegated_resident_resolves_resident_tier(tmp_path, monkeypatch):
    """Sol #12: delegate_to_agent routes residents through
    _build_specialist_options; it must resolve the RESIDENT tier (via
    AgentRegistry.tier_for_role) — hardcoding specialist: dropped a delegated
    resident's resident:<role> plugins."""
    import tools as tools_mod
    store = tmp_path / "store"
    e = entry("butlerplug", ["resident:butler"])
    art = mk_artifact(store, "butlerplug", e["artifact_id"])
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(
        tools_mod, "_agent_registry",
        SimpleNamespace(tier_for_role=lambda r: "resident" if r == "butler"
                        else None), raising=False)
    opts = tools_mod._build_specialist_options(_spec_cfg("butler"))
    assert opts.plugins == [{"type": "local", "path": str(art)}]


def test_specialist_and_executor_options_inject_plugins_guard(tmp_path,
                                                              monkeypatch):
    """Sol #5: the /config/plugins + settings.json guard is injected code-side
    into specialist AND executor options — not just residents."""
    import tools as tools_mod
    store = tmp_path / "store"
    e = entry("p", ["specialist:finance"])
    mk_artifact(store, "p", e["artifact_id"])
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)

    def _has_guard(opts):
        pre = (opts.hooks or {}).get("PreToolUse", [])
        return any(getattr(m, "matcher", None)
                   == "Write|Edit|MultiEdit|NotebookEdit|Bash" for m in pre)

    assert _has_guard(tools_mod._build_specialist_options(_spec_cfg("finance")))
    assert _has_guard(tools_mod._build_executor_options(
        _exec_defn(), executor_type="probe-exec"))


def test_resolution_from_recorded_builds_and_fails_closed(tmp_path):
    """Sol round-3 H7b: a resumed specialist rebuilds a resolution from its
    RECORDED artifacts (paths + derived grants), failing closed if a recorded
    path is gone."""
    import pytest
    import tools as tools_mod
    store = tmp_path / "store"
    e = entry("p", ["specialist:finance"])
    art = mk_artifact(store, "p", e["artifact_id"], mcp_servers={"p": {}})
    recorded = [{"name": "p", "artifact_id": e["artifact_id"], "path": str(art)}]
    res = tools_mod._resolution_from_recorded(recorded)
    assert [rp.path for rp in res.plugins] == [str(art)]
    assert res.plugins[0].version == "1.0.0"          # manifest read back
    with pytest.raises(RuntimeError):
        tools_mod._resolution_from_recorded(
            [{"name": "x", "artifact_id": "a", "path": "/nonexistent"}])


def test_build_specialist_options_uses_passed_resolution(tmp_path, monkeypatch):
    """Sol round-3 H7b: a caller-supplied resolution is used verbatim (no second
    resolve) so record + options can share one resolve."""
    import tools as tools_mod
    from plugin_registry import ResolutionResult, ResolvedPlugin
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(
        plugin_registry, "resolve_for",
        lambda t: (_ for _ in ()).throw(AssertionError("must not re-resolve")))
    rp = ResolvedPlugin(name="p", artifact_id="a" * 64, path="/store/p/a",
                        version="1", manifest={})
    opts = tools_mod._build_specialist_options(
        _spec_cfg("finance"),
        resolution=ResolutionResult(registry_valid=True, plugins=[rp]))
    assert opts.plugins == [{"type": "local", "path": "/store/p/a"}]


def test_resolution_from_recorded_empty_and_identity_mismatch(tmp_path):
    """Sol round-4: empty recorded → zero plugins (authoritative); a recorded
    artifact_id that disagrees with the on-disk metadata fails closed."""
    import pytest
    import tools as tools_mod
    assert tools_mod._resolution_from_recorded([]).plugins == []
    store = tmp_path / "store"
    e = entry("p", ["specialist:finance"])
    art = mk_artifact(store, "p", e["artifact_id"])
    with pytest.raises(RuntimeError):
        tools_mod._resolution_from_recorded(
            [{"name": "p", "artifact_id": "d" * 64, "path": str(art)}])


def test_resolution_from_recorded_malformed_protected_tools_excludes_one(
        tmp_path):
    """A:§3.7 (r2-B6/r3-4): a RECORDED legacy plugin with a malformed
    casa.protectedTools is excluded (per-plugin degradation, an issue is
    recorded) while a healthy sibling in the SAME recorded set still loads —
    never a whole-resume abort."""
    import tools as tools_mod
    store = tmp_path / "store"
    good_e = entry("good", ["specialist:finance"])
    good_art = mk_artifact(store, "good", good_e["artifact_id"],
                           mcp_servers={"good": {}})
    bad_e = entry("bad", ["specialist:finance"])
    bad_art = mk_artifact(
        store, "bad", bad_e["artifact_id"], mcp_servers={"bad": {}},
        extra_manifest={"casa": {"protectedTools": ["x", 1]}})
    recorded = [
        {"name": "good", "artifact_id": good_e["artifact_id"],
         "path": str(good_art)},
        {"name": "bad", "artifact_id": bad_e["artifact_id"],
         "path": str(bad_art)},
    ]
    res = tools_mod._resolution_from_recorded(recorded)
    assert [rp.name for rp in res.plugins] == ["good"]
    codes = {i.name: i.reason_code for i in res.issues}
    assert codes == {"bad": "protected_tools_invalid"}


# --- D2 (v0.74.0): one-assignment PluginBindingSnapshot ----------------------


def test_resolution_and_binding_publish_as_one_snapshot(tmp_path):
    """D2 torn-read fix: no state exists where _plugin_resolution is set
    while active_plugin_binding is stale — both derive from ONE frozen
    snapshot published by a single assignment."""
    store = tmp_path / "store"
    e = entry("probe", ["resident:assistant"])
    mk_artifact(store, "probe", e["artifact_id"])
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    a = _make_agent(tmp_path, role="assistant")

    async def run():
        assert a.plugin_binding_snapshot is None
        assert a._plugin_resolution is None
        assert a.active_plugin_binding == {}
        res = await a._get_plugin_resolution()
        snap = a.plugin_binding_snapshot
        assert snap is not None
        assert snap.resolution is res
        assert a._plugin_resolution is res
        assert a.active_plugin_binding == {"probe": e["artifact_id"]}
        assert dict(snap.binding) == {"probe": e["artifact_id"]}
        assert snap.generation == res.generation

    asyncio.run(run())


def test_binding_attrs_are_read_only(tmp_path):
    """The torn two-assignment publish is structurally impossible: the
    legacy attribute names are read-only properties."""
    a = _make_agent(tmp_path)
    with pytest.raises(AttributeError):
        a._plugin_resolution = object()
    with pytest.raises(AttributeError):
        a.active_plugin_binding = {"x": "y"}


def test_snapshot_and_binding_are_immutable(tmp_path):
    """S1: frozen dataclass + MappingProxyType binding."""
    import dataclasses
    reload_snapshot(registry_path=tmp_path / "absent.json",
                    store_root=tmp_path / "store")
    a = _make_agent(tmp_path)

    async def run():
        await a._get_plugin_resolution()
        snap = a.plugin_binding_snapshot
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.generation = 999
        with pytest.raises(TypeError):
            snap.binding["x"] = "y"          # MappingProxyType

    asyncio.run(run())


def test_tier_miss_resolve_logs_warning(tmp_path, caplog):
    """v0.74.1 (live finding): an Agent whose role the AgentRegistry does not
    know (e.g. a DISABLED specialist reconstructed by reload) silently
    resolved 'resident:<role>' — an empty, issueless resolution that looked
    like a healthy dormant agent. The fallback stays (back-compat) but must
    be LOUD."""
    import logging
    from agent_registry import AgentRegistry
    from config import AgentConfig
    reload_snapshot(registry_path=tmp_path / "absent.json",
                    store_root=tmp_path / "store")
    ar = AgentRegistry.build(residents={}, specialists={})   # knows nobody
    a = _make_agent(tmp_path, role="finance", agent_registry=ar)
    with caplog.at_level(logging.WARNING, logger="agent"):
        asyncio.run(a._get_plugin_resolution())
    assert any("tier" in rec.message and "finance" in rec.message
               for rec in caplog.records)
