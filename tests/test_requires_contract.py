"""Fail-closed dependency contract for delegated agents (spec A5).

A delegated agent that declares ``requires: {plugins, tools}`` in its
runtime.yaml must refuse to launch — typed ``dependency_unavailable`` —
when its required plugins/tools aren't ACTUALLY resolved, rather than
silently running from model memory. Covers the ``_prelaunch`` requires
gate (tools.py), the manifest-declared tool inventory helper
(plugin_grants.py), the runtime.yaml loader parse (agent_loader.py), and
the sync/interactive resolution-identity + no-side-effect contracts.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import AgentConfig, DelegateEntry, RequiresConfig
from plugin_registry import ResolutionResult, ResolvedPlugin

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

# Task N2's no-gap cutover removed the only bundled specialist (finance)
# from the image entirely — defaults/agents/specialists/ no longer exists,
# so TestLoaderParsing can no longer shutil.copytree the real shipped tree.
# Reuse the synthetic seed/role-artifact helpers already established for
# this in test_agent_loader.py (single implementation, no divergence).
try:
    from tests.test_agent_loader import _seed_role_artifact, _seed_specialist
except ImportError:
    from test_agent_loader import _seed_role_artifact, _seed_specialist

pytestmark = [pytest.mark.unit]


def _cfg(role: str, delegates: tuple[str, ...] = (),
         requires: RequiresConfig | None = None) -> AgentConfig:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role)
    cfg.delegates = [DelegateEntry(agent=d, purpose="p", when="w") for d in delegates]
    if requires is not None:
        cfg.requires = requires
    return cfg


def _resolution(*, name: str = "mtg", manifest: dict | None = None,
                 registry_valid: bool = True) -> ResolutionResult:
    rp = ResolvedPlugin(
        name=name, artifact_id="art1", path="/plugins/mtg",
        version="1.0.0", manifest=manifest or {},
    )
    return ResolutionResult(registry_valid=registry_valid, plugins=[rp])


class _FakeSDKClient:
    """No-op ClaudeSDKClient stand-in so a REAL _run_delegated_agent can be
    exercised (builder-identity assertions) without any SDK call."""
    def __init__(self, options): pass

    async def __aenter__(self): return self

    async def __aexit__(self, *exc): return False

    async def query(self, prompt): return None

    async def receive_response(self):
        if False:
            yield None
        return


def _count_resolve(monkeypatch, tm, resolution) -> list[str]:
    """Patch plugin_registry.resolve_for to record every target it is called
    with (and return *resolution*). Returns the growing list — a whole-path
    double-resolve shows up as more than one entry."""
    calls: list[str] = []

    def _resolve_for(target):
        calls.append(target)
        return resolution

    monkeypatch.setattr(tm.plugin_registry, "resolve_for", _resolve_for)
    return calls


def _init(monkeypatch, *, requires: RequiresConfig, resolution=None,
          channel: str = "telegram"):
    """Common init_tools + origin wiring for the _prelaunch requires-gate
    tests. Returns (tm, agent_mod, token) — caller resets the token."""
    import agent as agent_mod
    import tools as tm

    reg = MagicMock()
    reg.get.return_value = None
    reg.register_delegation = AsyncMock()
    reg.cancel_delegation = AsyncMock()
    reg.fail_delegation = AsyncMock()
    reg.complete_delegation = AsyncMock()

    tm.init_tools(
        channel_manager=MagicMock(), bus=MagicMock(),
        specialist_registry=reg, mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_role_map={
            "assistant": _cfg("assistant", delegates=("finance",)),
            "finance": _cfg("finance", requires=requires),
        },
    )
    if resolution is not None:
        monkeypatch.setattr(
            tm.plugin_registry, "resolve_for", lambda target: resolution,
        )
    token = agent_mod.origin_var.set({
        "role": "assistant", "execution_role": "assistant",
        "channel": channel, "chat_id": "c1", "cid": "t", "user_text": "hi",
    })
    return tm, agent_mod, token


class TestDeclaredToolsForResolution:
    def test_reads_provides_tools_from_manifest(self):
        from plugin_grants import declared_tools_for_resolution

        res = _resolution(manifest={
            "casa": {"provides_tools": ["mcp__plugin_mtg_mtg__lookup_rule"]},
        })
        assert declared_tools_for_resolution(res) == {
            "mcp__plugin_mtg_mtg__lookup_rule",
        }

    def test_manifest_without_provides_tools_returns_empty_set(self):
        from plugin_grants import declared_tools_for_resolution

        res = _resolution(manifest={"casa": {}})
        assert declared_tools_for_resolution(res) == set()

    def test_manifest_without_casa_key_returns_empty_set(self):
        from plugin_grants import declared_tools_for_resolution

        res = _resolution(manifest={})
        assert declared_tools_for_resolution(res) == set()

    def test_union_across_multiple_resolved_plugins(self):
        from plugin_grants import declared_tools_for_resolution

        rp1 = ResolvedPlugin(
            name="mtg", artifact_id="a1", path="/p/mtg", version="1",
            manifest={"casa": {"provides_tools": ["mcp__plugin_mtg_mtg__lookup_rule"]}},
        )
        rp2 = ResolvedPlugin(
            name="other", artifact_id="a2", path="/p/other", version="1",
            manifest={"casa": {"provides_tools": ["mcp__plugin_other_o__do_thing"]}},
        )
        res = ResolutionResult(registry_valid=True, plugins=[rp1, rp2])
        assert declared_tools_for_resolution(res) == {
            "mcp__plugin_mtg_mtg__lookup_rule",
            "mcp__plugin_other_o__do_thing",
        }

    # --- Fail-closed on malformed manifest metadata (r1-review) ---------
    # A malformed manifest must NEVER raise into _prelaunch and must NEVER
    # satisfy a tool requirement — it degrades to "no declared tools".

    def test_provides_tools_int_contributes_nothing(self):
        from plugin_grants import declared_tools_for_resolution

        # A non-list provides_tools (int) would raise TypeError under a naive
        # `for t in provided` — must degrade to empty, never raise.
        res = _resolution(manifest={"casa": {"provides_tools": 7}})
        assert declared_tools_for_resolution(res) == set()

    def test_provides_tools_dict_contributes_nothing(self):
        from plugin_grants import declared_tools_for_resolution

        # A dict would leak its KEYS under a naive iteration — a malformed
        # manifest could then satisfy a tool requirement. Must contribute
        # nothing.
        res = _resolution(manifest={
            "casa": {"provides_tools": {"mcp__plugin_mtg_mtg__lookup_rule": 1}},
        })
        assert declared_tools_for_resolution(res) == set()

    def test_provides_tools_list_of_non_strings_filtered(self):
        from plugin_grants import declared_tools_for_resolution

        # Non-string and empty-string entries are dropped; valid strings kept.
        res = _resolution(manifest={
            "casa": {"provides_tools": [
                "mcp__plugin_mtg_mtg__lookup_rule", 42, "", None,
                {"nested": "x"},
            ]},
        })
        assert declared_tools_for_resolution(res) == {
            "mcp__plugin_mtg_mtg__lookup_rule",
        }

    def test_non_dict_casa_contributes_nothing(self):
        from plugin_grants import declared_tools_for_resolution

        res = _resolution(manifest={"casa": ["not", "a", "dict"]})
        assert declared_tools_for_resolution(res) == set()

    def test_non_dict_manifest_contributes_nothing(self):
        from plugin_grants import declared_tools_for_resolution

        rp = ResolvedPlugin(
            name="mtg", artifact_id="a1", path="/p/mtg", version="1",
            manifest="not-a-dict",  # type: ignore[arg-type]
        )
        res = ResolutionResult(registry_valid=True, plugins=[rp])
        assert declared_tools_for_resolution(res) == set()


class TestLoaderParsing:
    def test_runtime_requires_parsed(self, tmp_path):
        specialists_dir = tmp_path / "agents" / "specialists"
        _seed_specialist(specialists_dir, "finance")
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "specialist", "finance")
        rt = specialists_dir / "finance" / "runtime.yaml"
        rt.write_text(
            rt.read_text()
            + "requires:\n  plugins: [mtg]\n  tools: [mcp__plugin_mtg_mtg__lookup_rule]\n"
        )
        from agent_loader import load_all_specialists

        found, failed = load_all_specialists(
            str(specialists_dir), roles_dir=str(roles_dir),
        )
        assert not failed
        assert found["finance"].requires.plugins == ["mtg"]
        assert found["finance"].requires.tools == ["mcp__plugin_mtg_mtg__lookup_rule"]

    def test_no_requires_block_defaults_empty(self, tmp_path):
        specialists_dir = tmp_path / "agents" / "specialists"
        _seed_specialist(specialists_dir, "finance")
        roles_dir = tmp_path / "roles"
        _seed_role_artifact(roles_dir, "specialist", "finance")
        from agent_loader import load_all_specialists

        found, failed = load_all_specialists(
            str(specialists_dir), roles_dir=str(roles_dir),
        )
        assert not failed
        assert found["finance"].requires.plugins == []
        assert found["finance"].requires.tools == []


@pytest.mark.asyncio
class TestPrelaunchRequiresGate:
    async def test_missing_plugin_denied(self, monkeypatch):
        requires = RequiresConfig(plugins=["mtg"], tools=[])
        # Resolution comes back with a DIFFERENT plugin than the one required.
        resolution = _resolution(name="other-plugin")
        tm, agent_mod, token = _init(
            monkeypatch, requires=requires, resolution=resolution,
        )
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "dependency_unavailable"
        assert payload["missing_plugins"] == ["mtg"]
        assert payload["missing_tools"] == []

    async def test_declared_but_absent_tool_denied(self, monkeypatch):
        tool_name = "mcp__plugin_mtg_mtg__lookup_rule"
        requires = RequiresConfig(plugins=[], tools=[tool_name])
        # Manifest declares the tool, but no .mcp.json server on disk means
        # grants_for_resolution (real, unpatched) yields no server grants —
        # the server-attachment half of the gate must still deny.
        resolution = _resolution(manifest={
            "casa": {"provides_tools": [tool_name]},
        })
        tm, agent_mod, token = _init(
            monkeypatch, requires=requires, resolution=resolution,
        )
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "dependency_unavailable"
        assert payload["missing_plugins"] == []
        assert payload["missing_tools"] == [tool_name]

    async def test_undeclared_tool_denied(self, monkeypatch):
        """The manifest doesn't declare the tool at all (empty provides_tools)
        — must land in missing_tools, isolated as the SOLE cause: the
        server grant IS present, so the ONLY reason for denial is the
        missing manifest declaration (r1-review Minor 1)."""
        tool_name = "mcp__plugin_mtg_mtg__lookup_rule"
        requires = RequiresConfig(plugins=[], tools=[tool_name])
        resolution = _resolution(manifest={"casa": {"provides_tools": []}})
        tm, agent_mod, token = _init(
            monkeypatch, requires=requires, resolution=resolution,
        )
        # Attach the tool's SERVER grant so the server-attachment half of
        # the gate PASSES — isolating "not manifest-declared" as the only
        # reason this tool is missing.
        monkeypatch.setattr(
            tm, "grants_for_resolution", lambda res: ["mcp__plugin_mtg_mtg"],
        )
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "dependency_unavailable"
        assert payload["missing_tools"] == [tool_name]

    async def test_registry_invalid_denied(self, monkeypatch):
        requires = RequiresConfig(plugins=["mtg"], tools=[])
        resolution = _resolution(name="mtg", registry_valid=False)
        tm, agent_mod, token = _init(
            monkeypatch, requires=requires, resolution=resolution,
        )
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "dependency_unavailable"
        assert payload["registry_valid"] is False

    @pytest.mark.parametrize("provides_tools", [
        7,                                       # non-list -> would raise
        {"mcp__plugin_mtg_mtg__lookup_rule": 1},  # dict -> would leak keys
        ["", None, 42],                          # list of non-strings/empties
        "not-a-list",                            # str -> also non-list
    ], ids=["int", "dict", "list-of-junk", "str"])
    async def test_malformed_provides_tools_denies_no_raise(
        self, monkeypatch, provides_tools,
    ):
        """A requires.tools that depends on a MALFORMED manifest must deny
        with dependency_unavailable — never raise, never satisfy (r1-review
        IMPORTANT). Even with the server grant present, a malformed
        provides_tools contributes NO declaration, so the tool is unmet."""
        tool_name = "mcp__plugin_mtg_mtg__lookup_rule"
        requires = RequiresConfig(plugins=[], tools=[tool_name])
        resolution = _resolution(manifest={
            "casa": {"provides_tools": provides_tools},
        })
        tm, agent_mod, token = _init(
            monkeypatch, requires=requires, resolution=resolution,
        )
        # Server grant IS present — isolate the malformed declaration as the
        # sole reason the tool is unmet (proves the manifest half fails
        # closed, not that the server half happened to also miss).
        monkeypatch.setattr(
            tm, "grants_for_resolution", lambda res: ["mcp__plugin_mtg_mtg"],
        )
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "dependency_unavailable"
        assert payload["missing_tools"] == [tool_name]

    async def test_non_dict_casa_denies_no_raise(self, monkeypatch):
        """A non-dict `casa` block must also degrade to no declarations →
        dependency_unavailable, no exception escaping _prelaunch."""
        tool_name = "mcp__plugin_mtg_mtg__lookup_rule"
        requires = RequiresConfig(plugins=[], tools=[tool_name])
        resolution = _resolution(manifest={"casa": ["not", "a", "dict"]})
        tm, agent_mod, token = _init(
            monkeypatch, requires=requires, resolution=resolution,
        )
        monkeypatch.setattr(
            tm, "grants_for_resolution", lambda res: ["mcp__plugin_mtg_mtg"],
        )
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "dependency_unavailable"
        assert payload["missing_tools"] == [tool_name]

    async def test_satisfied_launches_same_resolution_reaches_builder_once(
        self, monkeypatch,
    ):
        """Requires gate passes -> the SAME ResolutionResult object reaches
        the OPTIONS BUILDER (identity, not just equality), through the real
        _run_delegated_agent (not a mock), AND plugin_registry.resolve_for
        is called EXACTLY ONCE across the whole path — no double-resolve
        (r1-review Minor 2)."""
        tool_name = "mcp__plugin_mtg_mtg__lookup_rule"
        requires = RequiresConfig(plugins=["mtg"], tools=[tool_name])
        resolution = _resolution(manifest={
            "casa": {"provides_tools": [tool_name]},
        })
        tm, agent_mod, token = _init(monkeypatch, requires=requires)
        resolve_calls = _count_resolve(monkeypatch, tm, resolution)
        # Bypass the real filesystem/manifest checks for the server-grant
        # half of the gate (module-level import — monkeypatch-friendly per
        # the brief) so "satisfied" is deterministic without a real .mcp.json.
        monkeypatch.setattr(
            tm, "grants_for_resolution", lambda res: ["mcp__plugin_mtg_mtg"],
        )
        # Spy the real builder — assert the resolution reaches IT (not just a
        # mocked _run_delegated_agent), then short-circuit before any SDK.
        captured: dict = {}

        def _spy_builder(cfg, *, resolution=None, output_format=None):
            captured["resolution"] = resolution
            captured["output_format"] = output_format
            return MagicMock()

        monkeypatch.setattr(tm, "_build_specialist_options", _spy_builder)
        monkeypatch.setattr(tm, "ClaudeSDKClient", _FakeSDKClient)

        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "ok"
        assert captured["resolution"] is resolution
        assert captured["output_format"] is None
        assert resolve_calls == ["specialist:finance"]

    async def test_no_requires_skips_gate_no_resolve(self, monkeypatch):
        """cfg.requires empty (the default) -> the gate is skipped entirely;
        plugin_registry.resolve_for is never called for the requires check."""
        tm, agent_mod, token = _init(
            monkeypatch, requires=RequiresConfig(), resolution=None,
        )
        calls: list[str] = []
        monkeypatch.setattr(
            tm.plugin_registry, "resolve_for",
            lambda target: calls.append(target) or _resolution(),
        )

        async def _fake_run(
            cfg, task_text, context_text, resolution=None, output_format=None,
        ):
            assert output_format is None
            return tm.DelegatedOutput(text="ok")

        monkeypatch.setattr(tm, "_run_delegated_agent", _fake_run)

        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "ok"
        assert calls == []


@pytest.mark.asyncio
class TestInteractiveRequiresDenial:
    async def test_denied_requires_creates_no_topic_or_engagement(
        self, monkeypatch,
    ):
        """Task 4 deferred this sentinel to Task 5: an interactive delegation
        whose requires don't resolve must return dependency_unavailable and
        create NO topic/engagement record — the requires gate runs strictly
        before the interactive branch's side effects (spec A4 ordering,
        spec A5 gate)."""
        import agent as agent_mod
        import tools as tm

        requires = RequiresConfig(plugins=["mtg"], tools=[])
        resolution = _resolution(name="other-plugin")

        tch = MagicMock()
        tch.open_engagement_topic = AsyncMock(return_value=555)
        cm = MagicMock()
        cm.get.return_value = tch
        reg = MagicMock()
        reg.get.return_value = None
        eng_reg = MagicMock()
        eng_reg.create = AsyncMock()

        tm.init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=eng_reg,
            agent_role_map={
                "assistant": _cfg("assistant", delegates=("finance",)),
                "finance": _cfg("finance", requires=requires),
            },
        )
        monkeypatch.setattr(
            tm.plugin_registry, "resolve_for", lambda target: resolution,
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "execution_role": "assistant",
            "channel": "telegram", "chat_id": "c1", "cid": "t",
            "user_text": "hi",
        })
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "",
                "mode": "interactive",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "dependency_unavailable"
        assert payload["missing_plugins"] == ["mtg"]
        tch.open_engagement_topic.assert_not_awaited()
        eng_reg.create.assert_not_awaited()

    async def test_satisfied_interactive_reuses_same_resolution_once(
        self, monkeypatch,
    ):
        """Interactive success mirror of the sync builder-identity test: the
        requires gate passes, and the SAME ResolutionResult from _prelaunch
        reaches BOTH the engagement-record binding and the options builder,
        with plugin_registry.resolve_for called EXACTLY ONCE across the whole
        interactive path — no double-resolve (r1-review Minor 2)."""
        import agent as agent_mod
        import tools as tm

        tool_name = "mcp__plugin_mtg_mtg__lookup_rule"
        requires = RequiresConfig(plugins=["mtg"], tools=[tool_name])
        resolution = _resolution(manifest={
            "casa": {"provides_tools": [tool_name]},
        })

        tch = MagicMock()
        tch.engagement_permission_ok = True
        tch.engagement_supergroup_id = -1001
        tch.open_engagement_topic = AsyncMock(return_value=555)
        tch.set_channel_state = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = tch
        reg = MagicMock()
        reg.get.return_value = None
        eng_reg = MagicMock()
        rec = MagicMock()
        rec.id = "eng1"
        eng_reg.create = AsyncMock(return_value=rec)
        eng_reg.set_channel_state = AsyncMock()

        tm.init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=eng_reg,
            agent_role_map={
                "assistant": _cfg("assistant", delegates=("finance",)),
                "finance": _cfg("finance", requires=requires),
            },
        )
        resolve_calls = _count_resolve(monkeypatch, tm, resolution)
        monkeypatch.setattr(
            tm, "grants_for_resolution", lambda res: ["mcp__plugin_mtg_mtg"],
        )
        captured: dict = {}

        def _spy_builder(
            cfg, *, resolution=None, extra_casa_tools: tuple[str, ...] = (),
        ):
            captured["resolution"] = resolution
            captured["extra_casa_tools"] = extra_casa_tools
            opts = MagicMock()
            opts.allowed_tools = []
            return opts

        monkeypatch.setattr(tm, "_build_specialist_options", _spy_builder)
        driver = MagicMock()
        driver.start = AsyncMock()
        agent_mod.active_engagement_driver = driver

        token = agent_mod.origin_var.set({
            "role": "assistant", "execution_role": "assistant",
            "channel": "telegram", "chat_id": "c1", "cid": "t",
            "user_text": "hi",
        })
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "",
                "mode": "interactive",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "pending"
        # The engagement-record binding derives from the SAME resolution the
        # requires gate produced (identity check via the recorded artifacts).
        create_kwargs = eng_reg.create.await_args.kwargs
        assert create_kwargs["plugin_artifacts"] == tuple(
            {"name": rp.name, "artifact_id": rp.artifact_id, "path": rp.path,
             "manifest_name": rp.manifest_name}
            for rp in resolution.plugins
        )
        # The builder received the SAME object (identity, not equality).
        assert captured["resolution"] is resolution
        assert captured["extra_casa_tools"] == (
            "mcp__casa-framework__query_engager",
            "mcp__casa-framework__emit_completion",
        )
        # Exactly one resolve across the whole interactive path.
        assert resolve_calls == ["specialist:finance"]
