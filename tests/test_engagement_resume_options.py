"""Finding 2 (codex review, v0.69.10): a resumed interactive engagement must
carry the SAME full option set the initial start built — not a bare
ClaudeAgentOptions(resume=). InCasaDriver.resume() previously dropped
disallowed_tools (Agent/Task — Q-1), the fail-closed can_use_tool callback,
hooks (I-2 settings guard), skills, MCP restrictions, and cwd, so a resumed
specialist/executor ran on the CLI's broad default surface.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _wire(
    monkeypatch, *, specialist_cfg=None, executor_defn=None, mcp_registry=None,
):
    import tools as tools_mod
    import plugin_registry
    monkeypatch.setattr(
        tools_mod.plugin_registry, "resolve_for",
        lambda t: plugin_registry.ResolutionResult(registry_valid=True))
    monkeypatch.setattr("hooks.resolve_hooks", lambda *a, **kw: {})
    spec_reg = MagicMock()
    spec_reg.get = MagicMock(return_value=specialist_cfg)
    exec_reg = MagicMock()
    exec_reg.get = MagicMock(return_value=executor_defn)
    tools_mod.init_tools(
        channel_manager=MagicMock(), bus=MagicMock(),
        specialist_registry=spec_reg,
        mcp_registry=mcp_registry if mcp_registry is not None else MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        executor_registry=exec_reg,
    )
    return tools_mod


def _role_aware_mcp_registry():
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_sdk_factory(
        "casa-framework",
        lambda role, grants: {
            "type": "sdk",
            "instance": object(),
            "resolved_role": role,
            "resolved_grants": grants,
        },
    )
    return registry


def _specialist_cfg(tmp_home):
    return SimpleNamespace(
        role="finance", model="claude-sonnet-4-6", system_prompt="You are Alex.",
        tools=SimpleNamespace(allowed=["Read", "Skill"], disallowed=["Bash"],
                              permission_mode="acceptEdits", max_turns=10),
        mcp_server_names=[], hooks=SimpleNamespace(pre_tool_use=[]),
        cwd=tmp_home, memory=SimpleNamespace(token_budget=0),
    )


async def test_resume_options_rebuild_specialist_keeps_restrictions(tmp_path, monkeypatch):
    cfg = _specialist_cfg(str(tmp_path))
    cfg.mcp_server_names = ["casa-framework"]
    mcp = _role_aware_mcp_registry()
    tools_mod = _wire(
        monkeypatch, specialist_cfg=cfg, mcp_registry=mcp,
    )
    eng = SimpleNamespace(kind="specialist", role_or_type="finance")
    opts = tools_mod.build_engagement_resume_options(eng, "sess-xyz")
    assert opts.resume == "sess-xyz"
    assert "Agent" in opts.disallowed_tools and "Task" in opts.disallowed_tools
    assert opts.skills == "all"
    assert opts.can_use_tool is not None          # fail-closed callback restored
    assert "Skill" not in opts.allowed_tools
    assert {
        "mcp__casa-framework__query_engager",
        "mcp__casa-framework__emit_completion",
    } <= set(opts.allowed_tools)
    server = opts.mcp_servers["casa-framework"]
    assert server["resolved_role"] == "finance"
    assert {
        "mcp__casa-framework__query_engager",
        "mcp__casa-framework__emit_completion",
    } <= server["resolved_grants"]


async def test_resume_options_rebuild_executor(monkeypatch):
    defn = SimpleNamespace(
        hooks_path=None, mcp_server_names=["casa-framework"], tools_allowed=["Read"],
        model="claude-sonnet-4-6", permission_mode="auto", max_turns=None,
        tools_disallowed=[], driver="in_casa",
    )
    mcp = _role_aware_mcp_registry()
    tools_mod = _wire(
        monkeypatch, executor_defn=defn, mcp_registry=mcp,
    )
    eng = SimpleNamespace(kind="executor", role_or_type="configurator")
    opts = tools_mod.build_engagement_resume_options(eng, "sess-abc")
    assert opts.resume == "sess-abc"
    assert opts.skills == "all"
    assert {
        "mcp__casa-framework__query_engager",
        "mcp__casa-framework__emit_completion",
    } <= set(opts.allowed_tools)
    server = opts.mcp_servers["casa-framework"]
    assert server["resolved_role"] == "configurator"
    assert {
        "mcp__casa-framework__query_engager",
        "mcp__casa-framework__emit_completion",
    } <= server["resolved_grants"]


async def test_resume_options_missing_config_fails_closed(monkeypatch):
    """If the specialist/executor config is gone (removed since suspension),
    resume must FAIL rather than fall back to an unrestricted bare options."""
    tools_mod = _wire(monkeypatch, specialist_cfg=None)
    eng = SimpleNamespace(kind="specialist", role_or_type="ghost")
    with pytest.raises(Exception):
        tools_mod.build_engagement_resume_options(eng, "sess-1")


async def test_resume_options_executor_disallows_subagent_spawn_and_bash(monkeypatch):
    """Round-6 P0-1 (Sol): Q-1 — Agent/Task bypass allowed_tools and only
    disallowed_tools is CLI-enforced. A resumed executor built from a
    definition with disallowed: [] must still hard-deny Agent/Task, plus
    Bash when the clamped allowlist does not carry it (P0-2 belt)."""
    defn = SimpleNamespace(
        hooks_path=None, mcp_server_names=["casa-framework"],
        tools_allowed=["Read"], model="claude-sonnet-4-6",
        permission_mode="acceptEdits", max_turns=None,
        tools_disallowed=[], driver="in_casa",
    )
    tools_mod = _wire(
        monkeypatch, executor_defn=defn,
        mcp_registry=_role_aware_mcp_registry(),
    )
    eng = SimpleNamespace(kind="executor", role_or_type="configurator")
    opts = tools_mod.build_engagement_resume_options(eng, "sess-q1")
    assert {"Agent", "Task", "Bash"} <= set(opts.disallowed_tools)


async def test_resume_options_executor_keeps_legitimate_bash(monkeypatch):
    """plugin-developer-shaped resume: Bash in the (clamped) allowlist ->
    Agent/Task still denied, Bash not."""
    defn = SimpleNamespace(
        hooks_path=None, mcp_server_names=["casa-framework"],
        tools_allowed=["Read", "Bash"], model="claude-sonnet-4-6",
        permission_mode="acceptEdits", max_turns=None,
        tools_disallowed=[], driver="in_casa",
    )
    tools_mod = _wire(
        monkeypatch, executor_defn=defn,
        mcp_registry=_role_aware_mcp_registry(),
    )
    eng = SimpleNamespace(kind="executor", role_or_type="plugin-developer")
    opts = tools_mod.build_engagement_resume_options(eng, "sess-q1b")
    assert {"Agent", "Task"} <= set(opts.disallowed_tools)
    assert "Bash" not in opts.disallowed_tools
    assert "Bash" in opts.allowed_tools
