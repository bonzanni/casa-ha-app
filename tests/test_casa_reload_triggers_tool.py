"""Tests for casa_reload_triggers - in-process soft reload."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.asyncio


class TestCasaReloadTriggers:
    async def test_unknown_role_returns_error(self):
        from tools import casa_reload_triggers, init_tools
        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        )
        result = await casa_reload_triggers.handler({"role": "nope"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "unknown_role"

    async def test_no_trigger_registry_returns_error(self):
        from tools import casa_reload_triggers, init_tools
        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=None, engagement_registry=MagicMock(),
        )
        result = await casa_reload_triggers.handler({"role": "assistant"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_initialized"
