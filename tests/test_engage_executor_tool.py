"""Tests for engage_executor tool (Plan 3 real implementation)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _mock_executor_def(**overrides):
    from config import ExecutorDefinition
    defaults = {
        "type": "configurator",
        "description": "Test configurator type for engage_executor tests.",
        "model": "claude-sonnet-4-6",
        "driver": "in_casa",
        "enabled": True,
        "tools_allowed": ["Read"],
        "tools_disallowed": [],
        "permission_mode": "acceptEdits",
        "mcp_server_names": ["casa-framework"],
        "idle_reminder_days": 7,
        "prompt_template_path": "/tmp/nonexistent.md",
        "hooks_path": None,
        "observer_policy_path": None,
        "doctrine_dir": "/tmp/doctrine",
    }
    defaults.update(overrides)
    return ExecutorDefinition(**defaults)


async def _setup(
    executor_registry,
    channel_ok=True,
    prompt_template="You are {task}. Context: {context}. State: {world_state_summary}",
    tmp_path=None,
):
    from tools import init_tools
    if tmp_path is not None and executor_registry is not None:
        defn = executor_registry.get("configurator")
        if defn is not None:
            p = tmp_path / "prompt.md"
            p.write_text(prompt_template)
            defn.prompt_template_path = str(p)

    channel = MagicMock()
    channel.engagement_supergroup_id = -100123 if channel_ok else 0
    channel.engagement_permission_ok = channel_ok
    channel.open_engagement_topic = AsyncMock(return_value=42)
    channel.bot = MagicMock()
    channel.bot.edit_forum_topic = AsyncMock()
    cm = MagicMock()
    cm.get = MagicMock(return_value=channel)

    init_tools(
        channel_manager=cm, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        executor_registry=executor_registry,
    )
    return channel


class TestEngageExecutorReal:
    async def test_no_executor_types_when_registry_empty(self):
        from tools import engage_executor
        import agent as agent_mod

        reg = MagicMock()
        reg.get = MagicMock(return_value=None)
        reg.list_types = MagicMock(return_value=[])
        await _setup(reg)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "no_executor_types"

    async def test_unknown_type_error(self):
        from tools import engage_executor
        import agent as agent_mod

        reg = MagicMock()
        reg.get = MagicMock(return_value=None)
        reg.list_types = MagicMock(return_value=["other_type"])
        await _setup(reg)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "unknown_executor_type"

    async def test_engagement_not_configured(self):
        from tools import engage_executor
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])
        await _setup(reg, channel_ok=False)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "t", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "engagement_not_configured"

    async def test_happy_path_returns_pending(self, tmp_path, monkeypatch):
        from tools import engage_executor, init_tools
        import agent as agent_mod

        defn = _mock_executor_def()
        reg = MagicMock()
        reg.get = MagicMock(return_value=defn)
        reg.list_types = MagicMock(return_value=["configurator"])

        er = MagicMock()
        mock_rec = MagicMock()
        mock_rec.id = "abcd1234" + "0" * 24
        mock_rec.topic_id = 42
        er.create = AsyncMock(return_value=mock_rec)
        er.mark_error = AsyncMock()

        channel = await _setup(reg, tmp_path=tmp_path)
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=er,
            executor_registry=reg,
        )

        monkeypatch.setattr(agent_mod, "active_engagement_driver",
                            MagicMock(start=AsyncMock()), raising=False)

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            r = await engage_executor.handler({
                "executor_type": "configurator", "task": "make a thing",
                "context": "none",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(r["content"][0]["text"])
        assert payload["status"] == "pending"
        assert payload["executor_type"] == "configurator"
        assert payload["topic_id"] == 42

    async def test_requires_origin(self):
        from tools import engage_executor, init_tools
        reg = MagicMock()
        reg.list_types = MagicMock(return_value=[])
        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=MagicMock(),
            executor_registry=reg,
        )
        r = await engage_executor.handler({"executor_type": "configurator", "task": "t"})
        payload = json.loads(r["content"][0]["text"])
        assert payload["kind"] == "no_origin"


class TestEngageExecutorClaudeCode:
    @pytest.mark.skip(reason="TODO(Phase G): Full wiring test — covered by D-block E2E")
    async def test_dispatches_to_claude_code_driver(self, monkeypatch, tmp_path):
        """When executor.driver == 'claude_code', engage_executor calls the
        claude_code driver with the ExecutorDefinition as options."""
        # See TestEngageExecutorConfigurator for the setup pattern. The real
        # coverage lands in the E2E D-block against the mock CLI.
        pass
