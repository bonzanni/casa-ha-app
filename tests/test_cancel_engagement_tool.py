"""Tests for cancel_engagement (Ellen-callable)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


class TestCancelEngagement:
    async def test_cancels_known_engagement(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import cancel_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )
        tch = MagicMock(); tch.send_to_topic = AsyncMock(); tch.close_topic_with_check = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        res = await cancel_engagement.handler({"engagement_id": rec.id})
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "ok"
        assert rec.status == "cancelled"

    async def test_unknown_engagement_returns_error(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import cancel_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        res = await cancel_engagement.handler({"engagement_id": "nope"})
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "unknown_engagement"
