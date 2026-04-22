"""Tests for query_engager — retrieval + bounded LLM synthesis."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


class TestQueryEngager:
    async def test_returns_ok_when_memory_has_context(self, tmp_path, monkeypatch):
        from engagement_registry import EngagementRegistry
        from tools import query_engager, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram",
                              "chat_id": "c1"},
            topic_id=42,
        )
        memory = MagicMock()
        memory.get_context = AsyncMock(return_value="Lesina paid in March.")
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=MagicMock(), bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        # Inject memory provider via the well-known attribute
        import agent as agent_mod
        agent_mod.active_memory_provider = memory

        # Monkey-patch the constrained LLM helper
        async def _fake_synth(question, context, max_tokens):
            return "Yes, Lesina paid in March."
        monkeypatch.setattr("tools._synthesize_answer", _fake_synth)

        token = engagement_var.set(rec)
        try:
            res = await query_engager.handler({"question": "Did Lesina pay?",
                                                "max_tokens": 200})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "ok"
        assert "Lesina" in payload["text"]

    async def test_returns_unknown_when_synth_returns_unknown(self, tmp_path, monkeypatch):
        from engagement_registry import EngagementRegistry
        from tools import query_engager, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram",
                              "chat_id": "c1"},
            topic_id=42,
        )
        memory = MagicMock()
        memory.get_context = AsyncMock(return_value="")
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=MagicMock(), bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        import agent as agent_mod
        agent_mod.active_memory_provider = memory
        monkeypatch.setattr("tools._synthesize_answer", AsyncMock(return_value="UNKNOWN"))

        token = engagement_var.set(rec)
        try:
            res = await query_engager.handler({"question": "x"})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "unknown"

    async def test_returns_not_in_engagement_outside(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import query_engager, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        res = await query_engager.handler({"question": "x"})
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "not_in_engagement"
