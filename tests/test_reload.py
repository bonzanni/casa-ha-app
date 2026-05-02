"""Tests for reload.py dispatcher + per-scope handlers."""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _make_runtime():
    from runtime import CasaRuntime
    return CasaRuntime(
        agents={}, role_configs={}, specialist_registry=MagicMock(),
        executor_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_registry=MagicMock(), trigger_registry=MagicMock(),
        mcp_registry=MagicMock(), scope_registry=MagicMock(),
        session_registry=MagicMock(), channel_manager=MagicMock(),
        bus=MagicMock(), engagement_driver=MagicMock(),
        claude_code_driver=MagicMock(), memory_provider=MagicMock(),
        policy_lib=MagicMock(), base_memory=MagicMock(),
        config_dir="/x", agents_dir="/x/agents",
        home_root="/x/home", defaults_root="/opt/casa",
    )


class TestDispatchUnknownScope:
    async def test_returns_error_envelope(self):
        from reload import dispatch
        runtime = _make_runtime()
        result = await dispatch("nope", runtime=runtime)
        assert result["status"] == "error"
        assert result["kind"] == "unknown_scope"
        assert "nope" in result["message"]

    async def test_includes_scope_and_ms(self):
        from reload import dispatch
        runtime = _make_runtime()
        result = await dispatch("nope", runtime=runtime)
        assert result["scope"] == "nope"
        assert "ms" in result and result["ms"] >= 0


class TestReloadError:
    async def test_reload_error_caught_and_converted(self, monkeypatch):
        # Inject a fake handler that raises ReloadError; dispatch must
        # convert to result envelope.
        from reload import ReloadError, dispatch
        import reload as reload_mod

        async def boom(runtime, role=None):
            raise ReloadError("synthetic", "deliberate failure")

        monkeypatch.setitem(reload_mod._HANDLERS, "synthetic_scope", boom)
        runtime = _make_runtime()
        result = await dispatch("synthetic_scope", runtime=runtime)
        assert result["status"] == "error"
        assert result["kind"] == "synthetic"
        assert result["message"] == "deliberate failure"


class TestLockSerialization:
    async def test_same_scope_serializes(self, monkeypatch):
        # Two concurrent dispatches for the same scope must run sequentially.
        from reload import dispatch
        import reload as reload_mod

        ordering: list[str] = []

        async def slow_handler(runtime, role=None):
            ordering.append(f"start:{role}")
            await asyncio.sleep(0.05)
            ordering.append(f"end:{role}")
            return ["did_work"]

        monkeypatch.setitem(reload_mod._HANDLERS, "test_scope", slow_handler)
        # Both calls share the role-less lock-key for test_scope.
        runtime = _make_runtime()
        await asyncio.gather(
            dispatch("test_scope", runtime=runtime, role="a"),
            dispatch("test_scope", runtime=runtime, role="b"),
        )
        # No interleaving possible if locks work — second start AFTER first end.
        assert ordering[0].startswith("start:")
        assert ordering[1].startswith("end:")
        assert ordering[2].startswith("start:")
        assert ordering[3].startswith("end:")


class TestReloadTriggers:
    async def test_unknown_role_raises_load_error(self, tmp_path):
        from reload import dispatch, register_handler
        from reload import reload_triggers  # implemented below
        register_handler("triggers", reload_triggers)
        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(tmp_path / "agents")
        # No agents/<role>/ dir on disk
        result = await dispatch("triggers", runtime=runtime, role="ghost")
        assert result["status"] == "error"
        assert result["kind"] == "unknown_role"

    async def test_happy_path_calls_reregister(self, tmp_path, monkeypatch):
        from reload import dispatch, register_handler, reload_triggers
        register_handler("triggers", reload_triggers)
        # Set up a fake agent dir with minimal files agent_loader expects.
        agents_dir = tmp_path / "agents"
        ellen_dir = agents_dir / "ellen"
        ellen_dir.mkdir(parents=True)
        # Stub load_agent_from_dir + load_policies via monkeypatch.
        import reload as reload_mod
        from types import SimpleNamespace
        fake_cfg = SimpleNamespace(
            triggers=[SimpleNamespace(name="t1")],
            channels=["telegram"],
        )
        async def fake_load(*a, **kw): return None
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda *a, **kw: fake_cfg,
        )
        monkeypatch.setattr(
            "policies.load_policies",
            lambda *a, **kw: MagicMock(),
        )
        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.trigger_registry.reregister_for = MagicMock()

        result = await dispatch("triggers", runtime=runtime, role="ellen")
        assert result["status"] == "ok"
        runtime.trigger_registry.reregister_for.assert_called_once_with(
            "ellen", [fake_cfg.triggers[0]], ["telegram"],
        )


class TestReloadAgent:
    async def test_unknown_role_raises(self, tmp_path):
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)
        runtime = _make_runtime()
        runtime.agents_dir = str(tmp_path / "agents")
        result = await dispatch("agent", runtime=runtime, role="ghost")
        assert result["status"] == "error"
        assert result["kind"] == "unknown_role"

    async def test_resident_atomic_swap(self, tmp_path, monkeypatch):
        from reload import dispatch, register_handler, reload_agent
        from types import SimpleNamespace
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)

        # The new AgentConfig + Agent we'll observe post-swap.
        new_cfg = SimpleNamespace(role="ellen",
                                  character=SimpleNamespace(name="Ellen-2", card=""),
                                  triggers=[], channels=[])
        new_agent = MagicMock()
        new_agent.handle_message = MagicMock()

        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda *a, **kw: new_cfg,
        )
        monkeypatch.setattr(
            "policies.load_policies",
            lambda *a, **kw: MagicMock(),
        )
        # Patch reload_agent's Agent constructor to return our spy.
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent", lambda *a, **kw: new_agent)

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs["ellen"] = SimpleNamespace(
            role="ellen",
            character=SimpleNamespace(name="Ellen", card=""),
        )
        old_agent = MagicMock()
        runtime.agents["ellen"] = old_agent

        result = await dispatch("agent", runtime=runtime, role="ellen")
        assert result["status"] == "ok"
        # Atomic-swap completed.
        assert runtime.agents["ellen"] is new_agent
        # Bus was rebound.
        runtime.bus.register.assert_any_call("ellen", new_agent.handle_message)
        # role_configs updated.
        assert runtime.role_configs["ellen"] is new_cfg

    async def test_load_failure_leaves_runtime_untouched(self, tmp_path, monkeypatch):
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)
        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)

        def boom(*a, **kw):
            raise RuntimeError("yaml is broken")

        monkeypatch.setattr("agent_loader.load_agent_from_dir", boom)
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        old_agent = MagicMock()
        runtime.agents["ellen"] = old_agent

        result = await dispatch("agent", runtime=runtime, role="ellen")
        assert result["status"] == "error"
        assert result["kind"] == "load_error"
        # Old agent still in place.
        assert runtime.agents["ellen"] is old_agent


class TestReloadPolicies:
    async def test_rebuilds_scope_registry_and_swaps_agents(
        self, tmp_path, monkeypatch,
    ):
        from reload import dispatch, register_handler, reload_policies
        from types import SimpleNamespace
        register_handler("policies", reload_policies)

        # Stub the scope_registry rebuild path.
        new_scope_lib = MagicMock()
        new_scope_registry = MagicMock()
        new_scope_registry.prepare = MagicMock(return_value=asyncio.sleep(0))
        new_scope_registry._degraded = False

        new_policy_lib = MagicMock()

        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: new_policy_lib)
        monkeypatch.setattr("scope_registry.load_scope_library",
                            lambda *a, **kw: new_scope_lib)
        monkeypatch.setattr("scope_registry.ScopeRegistry",
                            lambda *a, **kw: new_scope_registry)
        # No-op for per-role re-load
        async def fake_dispatch(scope, *, runtime, role=None, **kw):
            return {"status": "ok", "actions": []}
        # Patch _reload_role_after_policies (helper) to avoid real load.
        import reload as reload_mod
        called_roles: list[str] = []
        async def fake_reload_role(runtime, role):
            called_roles.append(role)
        monkeypatch.setattr(reload_mod, "_reload_role_after_policies", fake_reload_role)

        runtime = _make_runtime()
        runtime.role_configs = {"ellen": MagicMock(), "tina": MagicMock()}
        runtime.specialist_registry.all_configs = lambda: {}

        result = await dispatch("policies", runtime=runtime)
        assert result["status"] == "ok"
        assert runtime.policy_lib is new_policy_lib
        assert runtime.scope_registry is new_scope_registry
        assert sorted(called_roles) == ["ellen", "tina"]


class TestReloadPluginEnv:
    async def test_resolves_and_pushes_to_environ(self, monkeypatch):
        from reload import dispatch, register_handler, reload_plugin_env
        register_handler("plugin_env", reload_plugin_env)

        monkeypatch.setattr("plugin_env_conf.read_entries",
                            lambda: {"FOO": "bar", "BAZ": "op://x"})
        monkeypatch.setattr("secrets_resolver.resolve",
                            lambda v: "RESOLVED" if "op://" in v else v)
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAZ", raising=False)

        runtime = _make_runtime()
        result = await dispatch("plugin_env", runtime=runtime)
        assert result["status"] == "ok"
        assert os.environ["FOO"] == "bar"
        assert os.environ["BAZ"] == "RESOLVED"

    async def test_removes_dropped_keys(self, monkeypatch):
        from reload import dispatch, register_handler, reload_plugin_env
        register_handler("plugin_env", reload_plugin_env)

        # First call: FOO + BAR present; remember snapshot.
        monkeypatch.setattr("plugin_env_conf.read_entries",
                            lambda: {"FOO": "1", "BAR": "2"})
        monkeypatch.setattr("secrets_resolver.resolve", lambda v: v)
        runtime = _make_runtime()
        await dispatch("plugin_env", runtime=runtime)
        assert os.environ.get("FOO") == "1"
        assert os.environ.get("BAR") == "2"

        # Second call: FOO only — BAR must be popped.
        monkeypatch.setattr("plugin_env_conf.read_entries",
                            lambda: {"FOO": "1"})
        await dispatch("plugin_env", runtime=runtime)
        assert os.environ.get("FOO") == "1"
        assert "BAR" not in os.environ
