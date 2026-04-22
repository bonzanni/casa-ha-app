"""Tests for the engage_executor framework tool (Plan 2 stub, Tier 3 types land Plan 3+)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


class TestEngageExecutorStub:
    async def test_returns_no_executor_types_in_plan2(self):
        import agent as agent_mod
        from tools import engage_executor, init_tools

        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        )

        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram",
            "chat_id": "c1", "cid": "x", "user_text": "hi",
        })
        try:
            result = await engage_executor.handler({
                "executor_type": "configurator", "task": "X", "context": "",
            })
        finally:
            agent_mod.origin_var.reset(token)
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "no_executor_types"
        assert "Plan 3" in payload["message"]

    async def test_requires_origin(self):
        from tools import engage_executor, init_tools

        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        )
        result = await engage_executor.handler({"executor_type": "x", "task": "t"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "no_origin"
