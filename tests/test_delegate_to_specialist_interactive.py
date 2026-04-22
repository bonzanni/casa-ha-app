"""Tests for delegate_to_specialist mode=interactive branch."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import (
    AgentConfig, CharacterConfig, MemoryConfig, SessionConfig, ToolsConfig,
)

pytestmark = pytest.mark.asyncio


def _make_alex_cfg():
    cfg = AgentConfig(role="finance")
    cfg.character = CharacterConfig(name="Alex", archetype="finance",
                                     card="", prompt="You are Alex.")
    cfg.enabled = True
    cfg.model = "sonnet"
    cfg.tools = ToolsConfig(allowed=["Read", "Write"], disallowed=[],
                            permission_mode="acceptEdits", max_turns=20)
    cfg.mcp_server_names = ["casa-framework"]
    cfg.memory = MemoryConfig(token_budget=0)
    cfg.session = SessionConfig(strategy="ephemeral", idle_timeout=0)
    cfg.channels = []
    cfg.system_prompt = "You are Alex."
    return cfg


class TestInteractiveMode:
    async def test_opens_topic_and_creates_engagement(self, tmp_path, monkeypatch):
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import delegate_to_specialist, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        tch = MagicMock()
        tch.engagement_permission_ok = True
        tch.engagement_supergroup_id = -1001
        tch.open_engagement_topic = AsyncMock(return_value=555)
        tch.send_to_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        specialist_reg = MagicMock()
        specialist_reg.get.return_value = _make_alex_cfg()
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=specialist_reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        # Driver + start side-effect
        driver = MagicMock()
        driver.start = AsyncMock()
        agent_mod.active_engagement_driver = driver

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
            "scope": "business",
        })
        try:
            res = await delegate_to_specialist.handler({
                "specialist": "finance", "task": "Plan Q2", "context": "",
                "mode": "interactive",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "pending"
        assert payload["topic_id"] == 555
        tch.open_engagement_topic.assert_awaited_once()
        driver.start.assert_awaited_once()
        assert reg.by_topic_id(555) is not None

    async def test_kind_engagement_not_configured_when_supergroup_empty(
        self, tmp_path, monkeypatch,
    ):
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import delegate_to_specialist, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        tch = MagicMock()
        tch.engagement_permission_ok = False
        tch.engagement_supergroup_id = 0
        cm = MagicMock(); cm.get.return_value = tch
        specialist_reg = MagicMock()
        specialist_reg.get.return_value = _make_alex_cfg()
        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=specialist_reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            res = await delegate_to_specialist.handler({
                "specialist": "finance", "task": "x", "context": "",
                "mode": "interactive",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "engagement_not_configured"
