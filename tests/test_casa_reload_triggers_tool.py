"""Tests for casa_reload_triggers - in-process soft reload (D.3 shim)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _runtime_with(tmp_path, *, trigger_registry=None):
    from runtime import CasaRuntime
    return CasaRuntime(
        agents={}, role_configs={}, specialist_registry=MagicMock(),
        executor_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_registry=MagicMock(),
        trigger_registry=trigger_registry,
        mcp_registry=MagicMock(), scope_registry=MagicMock(),
        session_registry=MagicMock(), channel_manager=MagicMock(),
        bus=MagicMock(), engagement_driver=MagicMock(),
        claude_code_driver=MagicMock(), memory_provider=MagicMock(),
        policy_lib=MagicMock(), base_memory=MagicMock(),
        config_dir=str(tmp_path),
        agents_dir=str(tmp_path / "agents"),
        home_root=str(tmp_path / "home"),
        defaults_root="/opt/casa",
    )


class TestCasaReloadTriggers:
    async def test_no_role_returns_role_required(self, tmp_path):
        import agent as agent_mod
        from tools import casa_reload_triggers
        agent_mod.active_runtime = _runtime_with(tmp_path, trigger_registry=MagicMock())
        result = await casa_reload_triggers.handler({})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "role_required"

    async def test_runtime_not_bound_returns_not_initialized(self):
        import agent as agent_mod
        from tools import casa_reload_triggers
        agent_mod.active_runtime = None
        result = await casa_reload_triggers.handler({"role": "any"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_initialized"

    async def test_unknown_role_returns_error(self, tmp_path):
        import agent as agent_mod
        from tools import casa_reload_triggers
        agent_mod.active_runtime = _runtime_with(tmp_path, trigger_registry=MagicMock())
        result = await casa_reload_triggers.handler({"role": "nope"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "unknown_role"

    async def test_no_trigger_registry_returns_error(self, tmp_path):
        import agent as agent_mod
        from tools import casa_reload_triggers
        # Create the agent dir so the unknown_role guard doesn't short-circuit;
        # then set trigger_registry=None and assert "not_initialized".
        agents_dir = tmp_path / "agents" / "assistant"
        agents_dir.mkdir(parents=True)
        agent_mod.active_runtime = _runtime_with(tmp_path, trigger_registry=None)
        result = await casa_reload_triggers.handler({"role": "assistant"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_initialized"
