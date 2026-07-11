"""P-5: plugin MCP-tool grants derived from installed state + fail-closed
can_use_tool. Namespace per code.claude.com/docs/en/mcp.md ("Plugin MCP tool
names"): mcp__plugin_<plugin>_<server>__<tool>; server-level grant drops the
__<tool> suffix (proven live on CC 2.1.150, 2026-07-12)."""
from __future__ import annotations

import json
import logging

import pytest

from plugin_grants import (
    derived_plugin_grants,
    grants_for_plugin,
    make_fail_closed_can_use_tool,
    sanitize_segment,
)

pytestmark = pytest.mark.unit


def _mk_plugin(cache_root, marketplace, name, version, mcp_servers):
    d = cache_root / marketplace / name / version
    d.mkdir(parents=True)
    if mcp_servers is not None:
        (d / ".mcp.json").write_text(
            json.dumps({"mcpServers": mcp_servers}), encoding="utf-8",
        )
    return d


def _mk_home(tmp_path, enabled: dict) -> str:
    home = tmp_path / "agent-home" / "finance"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": enabled}), encoding="utf-8",
    )
    return str(home)


def test_sanitize_keeps_hyphens_and_underscores():
    assert sanitize_segment("lesina-invoice") == "lesina-invoice"
    assert sanitize_segment("a_b-c9") == "a_b-c9"


def test_sanitize_replaces_other_chars():
    assert sanitize_segment("my plugin.v2") == "my_plugin_v2"


def test_grants_for_plugin_live_pin(tmp_path):
    """Pin the exact live-verified string (guards format drift)."""
    cache = tmp_path / "cache"
    _mk_plugin(cache, "casa-plugins", "lesina-invoice", "1.1.0",
               {"lesina-invoice": {"command": "node"}})
    assert grants_for_plugin(
        "lesina-invoice", "casa-plugins", cache_root=cache,
    ) == ["mcp__plugin_lesina-invoice_lesina-invoice"]


def test_grants_for_plugin_multi_server(tmp_path):
    cache = tmp_path / "cache"
    _mk_plugin(cache, "casa-plugins", "multi", "0.1.0",
               {"alpha": {}, "beta": {}})
    assert grants_for_plugin("multi", "casa-plugins", cache_root=cache) == [
        "mcp__plugin_multi_alpha", "mcp__plugin_multi_beta",
    ]


def test_grants_for_plugin_skill_only_is_empty(tmp_path):
    cache = tmp_path / "cache"
    _mk_plugin(cache, "casa-plugins", "skills-only", "0.1.0", None)
    assert grants_for_plugin("skills-only", "casa-plugins", cache_root=cache) == []


def test_grants_for_plugin_picks_highest_version(tmp_path):
    cache = tmp_path / "cache"
    _mk_plugin(cache, "casa-plugins", "p", "1.9.0", {"old": {}})
    _mk_plugin(cache, "casa-plugins", "p", "1.10.0", {"new": {}})
    assert grants_for_plugin("p", "casa-plugins", cache_root=cache) == [
        "mcp__plugin_p_new",
    ]


def test_grants_for_plugin_corrupt_mcp_json_degrades(tmp_path, caplog):
    cache = tmp_path / "cache"
    d = _mk_plugin(cache, "casa-plugins", "bad", "0.1.0", None)
    (d / ".mcp.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="plugin_grants"):
        assert grants_for_plugin("bad", "casa-plugins", cache_root=cache) == []
    assert any("bad" in r.message for r in caplog.records)


def test_derived_plugin_grants_unions_enabled_plugins(tmp_path):
    cache = tmp_path / "cache"
    _mk_plugin(cache, "casa-plugins", "lesina-invoice", "1.1.0",
               {"lesina-invoice": {}})
    _mk_plugin(cache, "other-mktpl", "second", "0.2.0", {"srv": {}})
    home = _mk_home(tmp_path, {
        "lesina-invoice@casa-plugins": True,
        "second@other-mktpl": True,
        "disabled@casa-plugins": False,
    })
    assert derived_plugin_grants(home, cache_root=cache) == [
        "mcp__plugin_lesina-invoice_lesina-invoice",
        "mcp__plugin_second_srv",
    ]


def test_derived_plugin_grants_no_settings_is_empty(tmp_path):
    assert derived_plugin_grants(
        str(tmp_path / "nope"), cache_root=tmp_path,
    ) == []


def test_derived_plugin_grants_corrupt_settings_degrades(tmp_path):
    home = tmp_path / "agent-home" / "x"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{broken", encoding="utf-8")
    assert derived_plugin_grants(str(home), cache_root=tmp_path) == []


async def test_fail_closed_callback_denies_with_log(caplog):
    from claude_agent_sdk import PermissionResultDeny
    cb = make_fail_closed_can_use_tool("finance")
    with caplog.at_level(logging.WARNING, logger="plugin_grants"):
        result = await cb("mcp__something__tool", {"x": 1}, None)
    assert isinstance(result, PermissionResultDeny)
    assert "mcp__something__tool" in result.message
    assert "finance" in result.message
    assert result.interrupt is False
    assert any("fail-closed" in r.message for r in caplog.records)


def _specialist_cfg(tmp_home: str):
    from types import SimpleNamespace
    return SimpleNamespace(
        role="finance",
        model="claude-sonnet-4-6",
        system_prompt="You are Alex.",
        tools=SimpleNamespace(
            allowed=["Read", "Skill"], disallowed=["Bash"],
            permission_mode="acceptEdits", max_turns=10,
        ),
        mcp_server_names=[],
        hooks=SimpleNamespace(pre_tool_use=[]),
        cwd=tmp_home,
        memory=SimpleNamespace(token_budget=0),
    )


def test_specialist_options_merge_grants_and_fail_closed(tmp_path, monkeypatch):
    import tools as tools_mod
    monkeypatch.setattr(
        tools_mod, "build_sdk_plugins", lambda **kw: [],
    )
    monkeypatch.setattr(
        "hooks.resolve_hooks", lambda *a, **kw: {},
    )
    monkeypatch.setattr(
        tools_mod, "derived_plugin_grants",
        lambda home, **kw: ["mcp__plugin_lesina-invoice_lesina-invoice"],
    )
    cfg = _specialist_cfg(str(tmp_path))
    opts = tools_mod._build_specialist_options(cfg)
    assert "mcp__plugin_lesina-invoice_lesina-invoice" in opts.allowed_tools
    # No duplicate if already present in cfg.tools.allowed:
    cfg2 = _specialist_cfg(str(tmp_path))
    cfg2.tools.allowed.append("mcp__plugin_lesina-invoice_lesina-invoice")
    opts2 = tools_mod._build_specialist_options(cfg2)
    assert opts2.allowed_tools.count(
        "mcp__plugin_lesina-invoice_lesina-invoice") == 1
    assert opts.can_use_tool is not None


def test_executor_options_keep_no_callback_and_no_grants(monkeypatch):
    """Spec B: executors are untouched — they have a real permission relay."""
    from types import SimpleNamespace
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "build_sdk_plugins", lambda **kw: [])
    monkeypatch.setattr("hooks.resolve_hooks", lambda *a, **kw: {})
    monkeypatch.setattr(
        tools_mod, "derived_plugin_grants",
        lambda home, **kw: ["mcp__plugin_should_not_appear"],
    )
    defn = SimpleNamespace(
        hooks_path=None, mcp_server_names=[], tools_allowed=["Read"],
        model="claude-sonnet-4-6", permission_mode="auto", max_turns=None,
        tools_disallowed=[],
    )
    opts = tools_mod._build_executor_options(defn)
    assert opts.can_use_tool is None
    assert "mcp__plugin_should_not_appear" not in opts.allowed_tools


def test_resident_options_merge_grants_and_fail_closed(tmp_path, monkeypatch):
    import asyncio
    import agent as agent_mod
    from agent import Agent
    from channels import ChannelManager
    from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
    from mcp_registry import McpServerRegistry
    from session_registry import SessionRegistry

    monkeypatch.setattr(agent_mod, "build_sdk_plugins", lambda **kw: [])
    monkeypatch.setattr(
        agent_mod, "derived_plugin_grants",
        lambda home, **kw: ["mcp__plugin_x_y"],
    )
    cfg = AgentConfig(
        role="assistant",
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
    )
    a = Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )

    async def run():
        opts = await a._build_options(
            channel="telegram", channel_key="k", is_fresh=False,
            resume_sid=None, user_text="hi",
        )
        assert "mcp__plugin_x_y" in opts.allowed_tools
        assert opts.can_use_tool is not None

    asyncio.run(run())
