"""Tests for reload.py dispatcher + per-scope handlers."""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _make_runtime():
    from runtime import CasaRuntime
    return CasaRuntime(
        agents={}, role_configs={}, specialist_registry=MagicMock(),
        executor_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_registry=MagicMock(), trigger_registry=MagicMock(),
        mcp_registry=MagicMock(),
        session_registry=MagicMock(), channel_manager=MagicMock(),
        bus=MagicMock(), engagement_driver=MagicMock(),
        claude_code_driver=MagicMock(),
        policy_lib=MagicMock(),
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

    async def test_updates_runtime_role_configs_for_resident(
        self, tmp_path, monkeypatch,
    ):
        """Q-1 regression: after dispatch('triggers'), the runtime
        role_configs cache MUST reflect the new cfg.triggers - not the
        boot-time list. Without this, tools.casa_reload_triggers
        returns a stale `registered` field that misleads the LLM.
        """
        from reload import dispatch, register_handler, reload_triggers
        register_handler("triggers", reload_triggers)

        agents_dir = tmp_path / "agents"
        ellen_dir = agents_dir / "ellen"
        ellen_dir.mkdir(parents=True)

        from types import SimpleNamespace
        boot_cfg = SimpleNamespace(
            triggers=[SimpleNamespace(name="boot-trigger")],
            channels=["telegram"],
        )
        new_cfg = SimpleNamespace(
            triggers=[
                SimpleNamespace(name="boot-trigger"),
                SimpleNamespace(name="probe-q1"),
            ],
            channels=["telegram"],
        )
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda *a, **kw: new_cfg,
        )
        monkeypatch.setattr(
            "policies.load_policies",
            lambda *a, **kw: MagicMock(),
        )
        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": boot_cfg}
        runtime.trigger_registry.reregister_for = MagicMock()

        result = await dispatch("triggers", runtime=runtime, role="ellen")

        assert result["status"] == "ok"
        assert runtime.role_configs["ellen"] is new_cfg
        names = [t.name for t in runtime.role_configs["ellen"].triggers]
        assert names == ["boot-trigger", "probe-q1"]

    async def test_specialist_branch_refreshes_registry(
        self, tmp_path, monkeypatch,
    ):
        """Q-1 specialist symmetry: when the role lives under
        agents/specialists/<role>/, dispatch('triggers') MUST trigger
        a SpecialistRegistry refresh so the back-compat consumer sees
        the post-reload state. Mirrors reload_agent's specialist branch
        at reload.py:341-348.

        (Specialists can't actually carry triggers per the file-set
        rules - this test asserts the codepath fires for symmetry.)
        """
        from reload import dispatch, register_handler, reload_triggers
        register_handler("triggers", reload_triggers)

        agents_dir = tmp_path / "agents"
        spec_dir = agents_dir / "specialists" / "finance"
        spec_dir.mkdir(parents=True)

        from types import SimpleNamespace
        new_cfg = SimpleNamespace(triggers=[], channels=[])
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda *a, **kw: new_cfg,
        )
        monkeypatch.setattr(
            "policies.load_policies",
            lambda *a, **kw: MagicMock(),
        )
        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": SimpleNamespace(triggers=[])}
        runtime.trigger_registry.reregister_for = MagicMock()
        runtime.specialist_registry.load = MagicMock()

        result = await dispatch("triggers", runtime=runtime, role="finance")

        assert result["status"] == "ok"
        assert "finance" not in runtime.role_configs
        runtime.specialist_registry.load.assert_called_once_with()


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
        old_agent.aclose = AsyncMock()
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

    async def test_scope_agent_provisions_agent_home_for_new_role(
        self, tmp_path, monkeypatch,
    ):
        """G-2 v0.37.7: configurator's
        ``casa_reload(scope=agent role=<new>)`` for a freshly-created
        specialist must produce ``<home_root>/<role>/.claude/settings.json``.

        Before the fix, only ``scope=agents`` (plural — the diff-based
        adds/evicts path) provisioned the agent-home; the granular
        per-role scope skipped it, and the first ``delegate_to_agent``
        failed with ``Working directory does not exist:
        /addon_configs/casa-agent/agent-home/<role>``. The fix moves
        provisioning into ``_construct_agent`` so it fires regardless of
        which scope triggered the construction.
        """
        from reload import dispatch, register_handler, reload_agent
        from types import SimpleNamespace
        register_handler("agent", reload_agent)

        # Disk layout: assistant (resident) + butler (resident) +
        # specialists/probe (the new role under test).
        agents_dir = tmp_path / "agents"
        (agents_dir / "assistant").mkdir(parents=True)
        (agents_dir / "butler").mkdir(parents=True)
        (agents_dir / "specialists" / "probe").mkdir(parents=True)
        home_root = tmp_path / "home"
        defaults_root = tmp_path / "defaults"

        new_cfg = SimpleNamespace(
            role="probe",
            character=SimpleNamespace(name="Probe", card=""),
            triggers=[], channels=[],
            memory=SimpleNamespace(read_strategy="per_turn"),
        )
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda *a, **kw: new_cfg,
        )
        monkeypatch.setattr(
            "policies.load_policies",
            lambda *a, **kw: MagicMock(),
        )
        # Let `_construct_agent` run for real (so provision_agent_home
        # fires). Stub the Agent class used inside.
        monkeypatch.setattr("agent.Agent", lambda **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.home_root = str(home_root)
        runtime.defaults_root = str(defaults_root)
        runtime.role_configs = {
            "assistant": MagicMock(), "butler": MagicMock(),
        }
        runtime.agents = {
            "assistant": MagicMock(), "butler": MagicMock(),
        }
        # specialist tier path through reload_agent.
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = lambda: {"probe": new_cfg}

        result = await dispatch("agent", runtime=runtime, role="probe")
        assert result["status"] == "ok", result
        # The fix: agent-home was provisioned even though scope=agent
        # (not scope=agents) triggered the construction.
        settings_path = home_root / "probe" / ".claude" / "settings.json"
        assert settings_path.is_file(), (
            f"expected agent-home settings.json at {settings_path}; "
            "scope=agent did not call provision_agent_home"
        )


class TestReloadPolicies:
    async def test_reloads_policy_lib_and_swaps_agents(
        self, tmp_path, monkeypatch,
    ):
        from reload import dispatch, register_handler, reload_policies
        register_handler("policies", reload_policies)

        new_policy_lib = MagicMock()

        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: new_policy_lib)
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


@pytest.mark.unit
class TestPluginEnvBootSeed:
    """M22 (v0.49.0): the boot path must seed _PLUGIN_ENV_LAST_KEYS.

    Pre-fix the snapshot started empty and only reload itself ever
    wrote it, so the FIRST casa_reload(scope='plugin_env') computed
    dropped = {} - new_keys = {} and could never remove a key that was
    applied at boot and has since been deleted from plugin-env.conf —
    a revoked plugin secret survived in os.environ (and kept being
    inherited by plugin MCP subprocesses) for the container's life.
    """

    async def test_first_reload_drops_key_applied_at_boot(self, monkeypatch):
        import reload as reload_mod
        from reload import dispatch, note_boot_plugin_env

        # Isolate the module-level snapshot from other tests.
        monkeypatch.setattr(reload_mod, "_PLUGIN_ENV_LAST_KEYS", set())
        # Simulate boot: the var was sourced into the process env and the
        # snapshot seeded (casa_core.main step 1b).
        monkeypatch.setenv("CASA_TEST_BOOT_SECRET", "s3cr3t")
        note_boot_plugin_env({"CASA_TEST_BOOT_SECRET"})
        # Operator then removed the line from plugin-env.conf.
        monkeypatch.setattr("plugin_env_conf.read_entries", lambda: {})
        monkeypatch.setattr("secrets_resolver.resolve", lambda v: v)

        result = await dispatch("plugin_env", runtime=_make_runtime())

        assert result["status"] == "ok"
        assert "dropped_1_vars" in result["actions"], (
            f"boot-applied key was not dropped: {result['actions']!r}"
        )
        assert "CASA_TEST_BOOT_SECRET" not in os.environ

    async def test_note_boot_plugin_env_copies_the_set(self, monkeypatch):
        """Mutating the caller's set afterwards must not leak into the
        snapshot."""
        import reload as reload_mod
        from reload import note_boot_plugin_env

        monkeypatch.setattr(reload_mod, "_PLUGIN_ENV_LAST_KEYS", set())
        keys = {"CASA_TEST_A"}
        note_boot_plugin_env(keys)
        keys.add("CASA_TEST_B")
        assert reload_mod._PLUGIN_ENV_LAST_KEYS == {"CASA_TEST_A"}


class TestReloadAgents:
    async def test_adds_new_resident(self, tmp_path, monkeypatch):
        from reload import dispatch, register_handler, reload_agents
        from types import SimpleNamespace
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "ellen").mkdir()       # known
        (agents_dir / "newcomer").mkdir()    # added on disk

        new_cfg = SimpleNamespace(
            role="newcomer",
            character=SimpleNamespace(name="Newcomer", card=""),
            triggers=[], channels=[],
            memory=SimpleNamespace(read_strategy="per_turn"),
        )

        def fake_load(d, **kw):
            return new_cfg if "newcomer" in d else MagicMock(role="ellen")

        monkeypatch.setattr("agent_loader.load_agent_from_dir", fake_load)
        monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
        provisioned: list[str] = []
        monkeypatch.setattr(
            "agent_home.provision_agent_home",
            lambda *, role, home_root, defaults_root: provisioned.append(role),
        )
        import reload as reload_mod
        monkeypatch.setattr(reload_mod, "_construct_agent",
                            lambda **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": MagicMock()}
        runtime.specialist_registry.all_configs = lambda: {}

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert "newcomer" in runtime.role_configs
        assert "newcomer" in runtime.agents
        assert "newcomer" in provisioned
        # ellen still there — no double-load
        assert "ellen" in runtime.role_configs

    async def test_evicts_deleted_resident(self, tmp_path, monkeypatch):
        """H11 (v0.49.0): eviction against the REAL MessageBus. The old
        version of this test used a MagicMock bus whose auto-created
        ``.unregister`` masked that MessageBus had no such method — the
        AttributeError was swallowed in prod and 'evicted' residents
        kept their queue + handler (ghost agents)."""
        from bus import MessageBus
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "ellen").mkdir()  # ellen still on disk
        # tina was in role_configs but no dir on disk → evict.

        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: MagicMock())
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        bus = MessageBus()
        bus.register("ellen", MagicMock())
        bus.register("tina", MagicMock())
        runtime.bus = bus
        runtime.role_configs = {"ellen": MagicMock(), "tina": MagicMock()}
        ellen_agent = MagicMock()
        ellen_agent.aclose = AsyncMock()
        tina_agent = MagicMock()
        tina_agent.aclose = AsyncMock()
        runtime.agents = {"ellen": ellen_agent, "tina": tina_agent}
        runtime.specialist_registry.all_configs = lambda: {}

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert "tina" not in runtime.role_configs
        assert "tina" not in runtime.agents
        # Real-bus teardown: queue + handler gone, triggers unwound.
        assert "tina" not in bus.queues
        assert "tina" not in bus.handlers
        runtime.trigger_registry.reregister_for.assert_any_call(
            "tina", [], [],
        )
        # Survivor untouched.
        assert "ellen" in bus.queues and "ellen" in bus.handlers

    async def test_surfaces_specialist_load_failures(
        self, tmp_path, monkeypatch,
    ):
        """O-2b (v0.37.9): when a specialist directory fails to load
        (e.g. missing required file), reload_agents must surface the
        failure in the returned actions list. Pre-v0.37.9 the registry
        swallowed LoadError via logger.error and reload returned
        ok=True with no trace of the failed specialist — operators
        running ``casactl reload --scope=agents`` had no way to know
        their new specialist did not land without grepping addon logs.

        Live evidence: 2026-05-14 P22 row 4 first attempt — probe22
        specialist missing response_shape.yaml + voice.yaml, registry
        logged ERROR but reload returned ok=True.
        """
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "ellen").mkdir()
        specialists_dir = agents_dir / "specialists"
        specialists_dir.mkdir()
        (specialists_dir / "broken").mkdir()  # no files — load will fail

        monkeypatch.setattr("agent_loader.load_agent_from_dir",
                            lambda *a, **kw: MagicMock(role="ellen"))
        monkeypatch.setattr("policies.load_policies",
                            lambda *a, **kw: MagicMock())

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": MagicMock()}
        runtime.agents = {"ellen": MagicMock()}

        # Real registry behavior: load() catches per-specialist LoadError
        # and records it as a load failure. The stub mimics this contract.
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = MagicMock(return_value={})
        runtime.specialist_registry.load_failures = MagicMock(return_value=[
            ("broken", "missing required file(s): ['runtime.yaml']"),
        ])

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        # Action trail must include a failed:<role>:<msg> entry.
        failed_actions = [
            a for a in result["actions"] if a.startswith("failed:")
        ]
        assert failed_actions, (
            f"reload_agents must surface specialist load failures in "
            f"actions; got {result['actions']!r}"
        )
        assert any("broken" in a for a in failed_actions)
        assert any("runtime.yaml" in a for a in failed_actions)


@pytest.mark.unit
class TestReloadBusLoopLifecycle:
    """H10 + H11 (v0.49.0): the resident add/remove lifecycle against a
    REAL MessageBus.

    Add half (H10): a resident added via reload must get a
    ``run_agent_loop`` consumer — pre-fix its queue was registered but
    never consumed, so every trigger firing / NOTIFICATION sat in the
    queue until the next add-on restart.

    Remove half (H11): an evicted resident's consumer must be cancelled
    and its queue/handler/triggers dropped — pre-fix the ghost kept
    consuming and executing scheduled prompts forever.
    """

    async def _drain_bus_loops(self, bus) -> None:
        """Cancel + await any consumers the real bus spawned (no leaks)."""
        tasks = list(getattr(bus, "_loop_tasks", {}).values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_message_to_reload_added_resident_is_dispatched(
        self, tmp_path, monkeypatch,
    ):
        from bus import BusMessage, MessageBus, MessageType
        import reload as reload_mod
        from reload import reload_agents

        agents_dir = tmp_path / "agents"
        (agents_dir / "newrole").mkdir(parents=True)

        received: list = []

        class _InertAgent:
            async def handle_message(self, msg):
                received.append(msg)
                return None

        monkeypatch.setattr(
            "policies.load_policies", lambda *a, **kw: MagicMock(),
        )
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir",
            lambda d, policies=None: MagicMock(role="newrole"),
        )
        monkeypatch.setattr(
            "agent_home.provision_agent_home", lambda **kw: None,
        )
        monkeypatch.setattr(
            reload_mod, "_construct_agent",
            lambda *, cfg, runtime: _InertAgent(),
        )

        runtime = _make_runtime()
        runtime.bus = MessageBus()  # REAL bus — the seam MagicMock hid
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.specialist_registry.all_configs = lambda: {}
        runtime.specialist_registry.load_failures = lambda: []

        try:
            actions = await reload_agents(runtime)
            assert "added_newrole" in actions

            await runtime.bus.send(BusMessage(
                type=MessageType.SCHEDULED, source="scheduler",
                target="newrole", content="cron tick",
            ))
            for _ in range(50):  # up to ~0.5s for the consumer to drain
                if received:
                    break
                await asyncio.sleep(0.01)

            assert received, (
                "SCHEDULED message to a reload-added resident was never "
                "dispatched: reload_agents registered the queue but "
                "spawned no run_agent_loop consumer (H10)"
            )
            assert received[0].content == "cron tick"
            assert runtime.bus.queues["newrole"].empty()
        finally:
            await self._drain_bus_loops(runtime.bus)

    async def test_evicted_resident_consumer_cancelled_no_ghost_turns(
        self, tmp_path, monkeypatch,
    ):
        from bus import BusMessage, MessageBus, MessageType
        from reload import reload_agents

        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)
        # tina: known to the runtime, deleted from disk → evict.

        monkeypatch.setattr(
            "policies.load_policies", lambda *a, **kw: MagicMock(),
        )
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir", lambda *a, **kw: MagicMock(),
        )

        ghost_turns: list = []

        async def tina_handler(msg):
            ghost_turns.append(msg)
            return None

        runtime = _make_runtime()
        bus = MessageBus()
        runtime.bus = bus
        bus.register("ellen", MagicMock())
        bus.register("tina", tina_handler)
        tina_task = bus.start_agent_loop("tina")  # boot-style live consumer
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.role_configs = {"ellen": MagicMock(), "tina": MagicMock()}
        ellen_agent = MagicMock()
        ellen_agent.aclose = AsyncMock()
        tina_agent = MagicMock()
        tina_agent.aclose = AsyncMock()
        runtime.agents = {"ellen": ellen_agent, "tina": tina_agent}
        runtime.specialist_registry.all_configs = lambda: {}
        runtime.specialist_registry.load_failures = lambda: []

        try:
            actions = await reload_agents(runtime)
            assert "evicted_tina" in actions

            # The consumer is cancelled AND awaited by the eviction path.
            assert tina_task.done() and tina_task.cancelled()
            assert "tina" not in bus.queues
            assert "tina" not in bus.handlers

            # A scheduler-style send after eviction is silently dropped —
            # no queueing, no ghost dispatch.
            await bus.send(BusMessage(
                type=MessageType.SCHEDULED, source="scheduler",
                target="tina", content="cron prompt",
            ))
            await asyncio.sleep(0.05)
            assert not ghost_turns, (
                "evicted resident processed a message (ghost agent)"
            )
            assert "tina" not in bus.queues

            # Trigger jobs + webhook allowlist unwound.
            runtime.trigger_registry.reregister_for.assert_any_call(
                "tina", [], [],
            )
        finally:
            await self._drain_bus_loops(bus)


class TestReloadFull:
    async def test_calls_other_handlers_in_order(self, monkeypatch):
        from reload import dispatch, register_handler, reload_full
        register_handler("full", reload_full)

        called: list[str] = []

        async def stub_policies(rt, *, role=None):
            called.append("policies"); return ["p"]

        async def stub_agents(rt, *, role=None):
            called.append("agents"); return ["a"]

        async def stub_agent(rt, *, role=None):
            called.append(f"agent:{role}"); return ["x"]

        async def stub_env(rt, *, role=None):
            called.append("plugin_env"); return ["e"]

        import reload as reload_mod
        monkeypatch.setitem(reload_mod._HANDLERS, "policies", stub_policies)
        monkeypatch.setitem(reload_mod._HANDLERS, "agents", stub_agents)
        monkeypatch.setitem(reload_mod._HANDLERS, "agent", stub_agent)
        monkeypatch.setitem(reload_mod._HANDLERS, "plugin_env", stub_env)
        monkeypatch.setitem(reload_mod._HANDLERS, "full", reload_full)

        runtime = _make_runtime()
        runtime.role_configs = {"ellen": MagicMock(), "tina": MagicMock()}
        runtime.specialist_registry.all_configs = lambda: {}

        result = await dispatch("full", runtime=runtime, include_env=False)
        assert result["status"] == "ok"
        # Policies first, then agents, then per-role agent reload.
        assert called[0] == "policies"
        assert called[1] == "agents"
        assert "agent:ellen" in called[2:]
        assert "agent:tina" in called[2:]
        assert "plugin_env" not in called  # include_env=False

    async def test_include_env_calls_plugin_env(self, monkeypatch):
        from reload import dispatch, register_handler, reload_full
        register_handler("full", reload_full)

        called: list[str] = []
        async def stub(name):
            async def _h(rt, *, role=None):
                called.append(name); return [name]
            return _h
        import reload as reload_mod
        for s in ("policies", "agents", "agent", "plugin_env"):
            monkeypatch.setitem(reload_mod._HANDLERS, s, await stub(s))
        monkeypatch.setitem(reload_mod._HANDLERS, "full", reload_full)

        runtime = _make_runtime()
        runtime.role_configs = {}
        runtime.specialist_registry.all_configs = lambda: {}

        await dispatch("full", runtime=runtime, include_env=True)
        assert "plugin_env" in called


@pytest.mark.unit
class TestConstructAgentMemoryWiring:
    """H9 (v0.49.0): reload._construct_agent must pass the runtime's
    semantic_memory into Agent(...).

    The v0.45.0 memory retirement removed the old ``memory=`` wiring here
    and never re-added the Hindsight seam, so every reload-constructed
    resident silently fell back to NoOpSemanticMemory (long-term amnesia
    + permanently lost retains) until the next add-on restart. The bug
    was invisible because every other reload test monkeypatches
    ``_construct_agent`` — this class deliberately runs the REAL factory
    and the REAL ``agent.Agent``.
    """

    async def test_reload_constructed_agent_reuses_runtime_semantic_memory(
        self, monkeypatch,
    ):
        import agent as agent_mod
        import reload as reload_mod
        from types import SimpleNamespace
        from semantic_memory import NoOpSemanticMemory

        # Keep the test hermetic: no home-provisioning I/O, no hook
        # resolution. Everything else in Agent.__init__ runs for real.
        monkeypatch.setattr(
            "agent_home.provision_agent_home", lambda **kw: None,
        )
        monkeypatch.setattr(
            agent_mod, "resolve_hooks", lambda *a, **kw: MagicMock(),
        )

        sentinel = object()  # stands in for HindsightSemanticMemory
        runtime = _make_runtime()
        runtime.semantic_memory = sentinel
        cfg = SimpleNamespace(role="probe", hooks=None, cwd="")

        constructed = reload_mod._construct_agent(cfg=cfg, runtime=runtime)

        assert constructed._semantic_memory is sentinel, (
            "reload._construct_agent dropped semantic_memory — reloaded "
            "agents silently fall back to NoOpSemanticMemory (long-term "
            "amnesia + lost retains)"
        )
        assert not isinstance(constructed._semantic_memory, NoOpSemanticMemory)

    async def test_runtime_without_semantic_memory_falls_back_to_noop(
        self, monkeypatch,
    ):
        """Contract pin: a runtime whose ``semantic_memory`` is unset
        (the CasaRuntime default, and every MagicMock-light test
        stand-in) still constructs — Agent maps None to NoOp."""
        import agent as agent_mod
        import reload as reload_mod
        from types import SimpleNamespace
        from semantic_memory import NoOpSemanticMemory

        monkeypatch.setattr(
            "agent_home.provision_agent_home", lambda **kw: None,
        )
        monkeypatch.setattr(
            agent_mod, "resolve_hooks", lambda *a, **kw: MagicMock(),
        )

        runtime = _make_runtime()  # semantic_memory left at its default
        cfg = SimpleNamespace(role="probe", hooks=None, cwd="")

        constructed = reload_mod._construct_agent(cfg=cfg, runtime=runtime)

        assert isinstance(constructed._semantic_memory, NoOpSemanticMemory)


@pytest.mark.unit
class TestFullScopeExclusion:
    """M21 (v0.49.0): the _RWLock writer must actually exclude readers.

    Pre-fix, acquire_write recorded no lock state at all, so a
    scope='full' reload was NOT mutually exclusive with other scopes —
    a concurrent scope='agent' dispatch interleaved with reload_full's
    multi-step mutation of runtime.agents / role_configs /
    agent_registry, directly contradicting the module docstring.
    """

    async def test_full_excludes_other_scopes(self, monkeypatch):
        """A reader arriving while the writer is active must wait for
        release_write. Pre-fix ordering was
        ["full:start", "agent:start", "agent:end", "full:end"]."""
        import reload as reload_mod
        from reload import dispatch

        ordering: list[str] = []

        async def slow_full(runtime, *, role=None, include_env=False):
            ordering.append("full:start")
            await asyncio.sleep(0.05)  # yield so a reader could sneak in
            ordering.append("full:end")
            return ["full_work"]

        async def fast_agent(runtime, *, role=None):
            ordering.append("agent:start")
            ordering.append("agent:end")
            return ["agent_work"]

        monkeypatch.setitem(reload_mod._HANDLERS, "full", slow_full)
        monkeypatch.setitem(reload_mod._HANDLERS, "agent", fast_agent)
        # Fresh RW lock bound to THIS test's event loop.
        monkeypatch.setattr(reload_mod, "_GLOBAL_RW", None)
        runtime = _make_runtime()

        async def run_agent_later():
            await asyncio.sleep(0.01)  # start after full holds the write lock
            return await dispatch("agent", runtime=runtime, role="x")

        results = await asyncio.gather(
            dispatch("full", runtime=runtime),
            run_agent_later(),
        )
        assert all(r["status"] == "ok" for r in results), results
        assert ordering == [
            "full:start", "full:end", "agent:start", "agent:end",
        ], (
            f"scope='full' did not exclude scope='agent': {ordering!r} — "
            "the reader ran inside the writer's critical section"
        )

    async def test_reader_in_flight_blocks_writer(self, monkeypatch):
        """The opposite direction already worked pre-fix (writer waits
        for _readers == 0) and must not regress."""
        import reload as reload_mod
        from reload import dispatch

        ordering: list[str] = []

        async def slow_agent(runtime, *, role=None):
            ordering.append("agent:start")
            await asyncio.sleep(0.05)
            ordering.append("agent:end")
            return ["agent_work"]

        async def fast_full(runtime, *, role=None, include_env=False):
            ordering.append("full:start")
            ordering.append("full:end")
            return ["full_work"]

        monkeypatch.setitem(reload_mod._HANDLERS, "agent", slow_agent)
        monkeypatch.setitem(reload_mod._HANDLERS, "full", fast_full)
        monkeypatch.setattr(reload_mod, "_GLOBAL_RW", None)
        runtime = _make_runtime()

        async def run_full_later():
            await asyncio.sleep(0.01)  # start after agent holds a read lock
            return await dispatch("full", runtime=runtime)

        results = await asyncio.gather(
            dispatch("agent", runtime=runtime, role="x"),
            run_full_later(),
        )
        assert all(r["status"] == "ok" for r in results), results
        assert ordering == [
            "agent:start", "agent:end", "full:start", "full:end",
        ]


class TestExecutorsScope:
    """A-1: 7th reload scope for ExecutorRegistry."""

    async def test_executors_scope_calls_registry_load(self):
        from reload import dispatch
        runtime = _make_runtime()
        # Drive the registry mock to assert load() was awaited.
        runtime.executor_registry.load = MagicMock()
        result = await dispatch("executors", runtime=runtime)
        assert result["status"] == "ok"
        assert result["scope"] == "executors"
        # O-2a (v0.37.9): rebuild_executor_registry is first; per-resident
        # reload_agent fan-out follows so cached system-prompt state
        # regenerates. role_configs is empty in this fixture, so the only
        # action remains the registry rebuild.
        assert result["actions"] == ["rebuild_executor_registry"]
        runtime.executor_registry.load.assert_called_once()

    async def test_executors_scope_fans_out_to_residents(self, monkeypatch):
        """O-2a (v0.37.9): after rebuilding the executor registry,
        reload_executors must call ``reload_agent`` for each resident so
        the resident's cached ``<executors>`` system-prompt block (built
        from a snapshot at construct_agent time) regenerates. Without
        this, an operator who flips ``enabled: true`` on a previously-
        disabled executor and runs ``casactl reload --scope=executors``
        sees the registry rebuild but Ellen's prompt block stays stale
        until the next ``--scope=agent`` fires.

        Live evidence: 2026-05-14 P22 row5b — Ellen replied "No" to
        "is plugin-developer enabled?" between an executor scope reload
        and the next agent-scope reload, contradicting the live
        registry state.
        """
        from reload import dispatch
        import reload as reload_mod

        # Stub the executors handler so it just records and returns the
        # registry-rebuild action; we want to assert the fan-out shape
        # at the dispatcher level, not re-run executor_registry.load.
        runtime = _make_runtime()
        runtime.role_configs = {"assistant": MagicMock(), "butler": MagicMock()}

        fan_out_calls: list[str] = []

        async def fake_agent(rt, role=None):
            fan_out_calls.append(role)
            return ["load_config", "construct_agent", "reregister_bus"]

        monkeypatch.setitem(reload_mod._HANDLERS, "agent", fake_agent)
        runtime.executor_registry.load = MagicMock()

        result = await dispatch("executors", runtime=runtime)

        assert result["status"] == "ok"
        # Each resident must have been refreshed.
        assert sorted(fan_out_calls) == ["assistant", "butler"]
        # Action trail surfaces the per-resident sub-actions with prefix.
        assert "rebuild_executor_registry" in result["actions"]
        for role in ("assistant", "butler"):
            for sub in ("load_config", "construct_agent", "reregister_bus"):
                assert f"agent:{role}:{sub}" in result["actions"], (
                    f"expected agent:{role}:{sub} in actions, got "
                    f"{result['actions']!r}"
                )

    async def test_executors_load_raises_becomes_load_error(self):
        from reload import dispatch
        runtime = _make_runtime()
        runtime.executor_registry.load = MagicMock(
            side_effect=RuntimeError("synthetic")
        )
        result = await dispatch("executors", runtime=runtime)
        assert result["status"] == "error"
        assert result["kind"] == "load_error"
        assert "synthetic" in result["message"]

    async def test_full_scope_includes_executors_rebuild(self, monkeypatch):
        """reload_full chains executors BEFORE per-role agent reload."""
        from reload import dispatch
        import reload as reload_mod

        # Capture handler invocation order.
        order: list[str] = []

        async def fake_policies(runtime, role=None):
            order.append("policies")
            return ["pol"]

        async def fake_agents(runtime, role=None):
            order.append("agents")
            return ["ag"]

        async def fake_executors(runtime, role=None):
            order.append("executors")
            return ["rebuild_executor_registry"]

        async def fake_agent(runtime, role=None):
            order.append(f"agent:{role}")
            return ["a_load"]

        monkeypatch.setitem(reload_mod._HANDLERS, "policies", fake_policies)
        monkeypatch.setitem(reload_mod._HANDLERS, "agents", fake_agents)
        monkeypatch.setitem(reload_mod._HANDLERS, "executors", fake_executors)
        monkeypatch.setitem(reload_mod._HANDLERS, "agent", fake_agent)

        runtime = _make_runtime()
        runtime.role_configs = {"assistant": MagicMock()}
        runtime.specialist_registry.all_configs = MagicMock(return_value={})

        result = await dispatch("full", runtime=runtime)
        assert result["status"] == "ok"
        # executors must precede per-role agent reload.
        executors_idx = order.index("executors")
        agent_idx = order.index("agent:assistant")
        assert executors_idx < agent_idx
        assert any(
            a == "executors:rebuild_executor_registry"
            for a in result["actions"]
        )


class TestReloadRefreshesDelegationRoleMap:
    """P-6 (live run 2026-07-11): ``tools._agent_role_map`` is populated once
    at boot and no reload handler refreshed it, so ``delegate_to_agent``
    resolved BOOT-TIME AgentConfigs forever — a specialist ``tools.allowed``
    grant (lesina-invoice) stayed inert for fresh delegations until a full
    add-on restart, while ``casa_reload`` reported ok=True."""

    async def test_agent_scope_specialist_reload_refreshes_role_map(
        self, tmp_path, monkeypatch,
    ):
        from types import SimpleNamespace
        import tools as tools_mod
        from reload import dispatch, register_handler, reload_agent
        register_handler("agent", reload_agent)

        agents_dir = tmp_path / "agents"
        (agents_dir / "specialists" / "finance").mkdir(parents=True)

        new_cfg = SimpleNamespace(
            role="finance",
            character=SimpleNamespace(name="Alex", card=""),
            triggers=[], channels=[],
        )
        new_agent = MagicMock()
        monkeypatch.setattr(
            "agent_loader.load_agent_from_dir", lambda *a, **kw: new_cfg,
        )
        monkeypatch.setattr(
            "policies.load_policies", lambda *a, **kw: MagicMock(),
        )
        import reload as reload_mod
        monkeypatch.setattr(
            reload_mod, "_construct_agent", lambda *a, **kw: new_agent,
        )

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.all_configs = MagicMock(
            return_value={"finance": new_cfg},
        )

        # Boot-time wiring left the OLD config in the delegation map.
        old_cfg = SimpleNamespace(role="finance")
        monkeypatch.setattr(tools_mod, "_agent_role_map", {"finance": old_cfg})

        result = await dispatch("agent", runtime=runtime, role="finance")
        assert result["status"] == "ok"
        # The delegation-resolution map must now hold the POST-reload config.
        assert tools_mod._agent_role_map["finance"] is new_cfg
        assert "refresh_role_map" in result["actions"]

    async def test_agents_scope_sweep_refreshes_role_map(
        self, tmp_path, monkeypatch,
    ):
        from types import SimpleNamespace
        import tools as tools_mod
        from reload import dispatch, register_handler, reload_agents
        register_handler("agents", reload_agents)

        agents_dir = tmp_path / "agents"
        (agents_dir / "ellen").mkdir(parents=True)
        monkeypatch.setattr(
            "policies.load_policies", lambda *a, **kw: MagicMock(),
        )

        runtime = _make_runtime()
        runtime.config_dir = str(tmp_path)
        runtime.agents_dir = str(agents_dir)
        resident_cfg = SimpleNamespace(
            role="ellen",
            character=SimpleNamespace(name="Ellen", card=""),
            triggers=[], channels=["telegram"],
        )
        runtime.role_configs["ellen"] = resident_cfg
        spec_cfg = SimpleNamespace(
            role="finance",
            character=SimpleNamespace(name="Alex", card=""),
            triggers=[], channels=[],
        )
        runtime.specialist_registry.load = MagicMock()
        runtime.specialist_registry.load_failures = MagicMock(return_value=[])
        runtime.specialist_registry.all_configs = MagicMock(
            return_value={"finance": spec_cfg},
        )

        monkeypatch.setattr(tools_mod, "_agent_role_map", {})

        result = await dispatch("agents", runtime=runtime)
        assert result["status"] == "ok"
        assert tools_mod._agent_role_map == {
            "ellen": resident_cfg, "finance": spec_cfg,
        }
