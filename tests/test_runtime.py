"""Tests for CasaRuntime dataclass."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_casa_runtime_holds_all_required_fields():
    from runtime import CasaRuntime

    rt = CasaRuntime(
        agents={},
        role_configs={},
        specialist_registry=MagicMock(),
        executor_registry=MagicMock(),
        engagement_registry=MagicMock(),
        agent_registry=MagicMock(),
        trigger_registry=MagicMock(),
        mcp_registry=MagicMock(),
        scope_registry=MagicMock(),
        session_registry=MagicMock(),
        channel_manager=MagicMock(),
        bus=MagicMock(),
        engagement_driver=MagicMock(),
        claude_code_driver=MagicMock(),
        memory_provider=MagicMock(),
        policy_lib=MagicMock(),
        base_memory=MagicMock(),
        config_dir="/addon_configs/casa-agent",
        agents_dir="/addon_configs/casa-agent/agents",
        home_root="/addon_configs/casa-agent/agent-home",
        defaults_root="/opt/casa",
    )
    assert rt.config_dir == "/addon_configs/casa-agent"
    assert rt.agents == {}


def test_casa_runtime_agents_is_mutable():
    from runtime import CasaRuntime

    rt = CasaRuntime(
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
    rt.agents["ellen"] = MagicMock()
    assert "ellen" in rt.agents
