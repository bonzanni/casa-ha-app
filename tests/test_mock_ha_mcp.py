"""Smoke tests for the mock HA MCP server used by test_ha_delegation.sh."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp.test_utils import AioHTTPTestCase

pytestmark = pytest.mark.unit

# Add test-local/e2e/ to sys.path so we can import the mock package.
_E2E_ROOT = Path(__file__).resolve().parents[1] / "test-local" / "e2e"
if str(_E2E_ROOT) not in sys.path:
    sys.path.insert(0, str(_E2E_ROOT))

from mock_ha_mcp.server import build_app  # noqa: E402


class TestMockHaMcp(AioHTTPTestCase):
    async def get_application(self):
        return build_app()

    async def test_initialize_returns_serverinfo(self):
        resp = await self.client.post("/", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}
        })
        body = await resp.json()
        assert body["result"]["serverInfo"]["name"] == "homeassistant-mock"

    async def test_tools_list_returns_three_tools(self):
        with patch.dict(os.environ):
            os.environ.pop("MOCK_HA_MALFORMED_TOOL", None)
            resp = await self.client.post("/", json={
                "jsonrpc": "2.0", "id": 2,
                "method": "tools/list", "params": {},
            })
        body = await resp.json()
        names = {t["name"] for t in body["result"]["tools"]}
        assert names == {"HassTurnOn", "HassTurnOff", "GetLiveContext"}

    async def test_tools_list_can_opt_in_to_one_malformed_schema(self):
        with patch.dict(os.environ, {"MOCK_HA_MALFORMED_TOOL": "1"}):
            resp = await self.client.post("/", json={
                "jsonrpc": "2.0", "id": 21,
                "method": "tools/list", "params": {},
            })
        body = await resp.json()
        tools = {tool["name"]: tool for tool in body["result"]["tools"]}
        assert set(tools) == {
            "HassTurnOn",
            "HassTurnOff",
            "GetLiveContext",
            "MalformedSchema",
        }
        assert tools["MalformedSchema"]["inputSchema"] == "not-an-object"

    async def test_tools_call_records_invocation(self):
        await self.client.post("/_reset")

        resp = await self.client.post("/", json={
            "jsonrpc": "2.0", "id": 3,
            "method": "tools/call",
            "params": {"name": "HassTurnOff", "arguments": {"name": "kitchen"}},
        })
        body = await resp.json()
        assert "result" in body

        calls_resp = await self.client.get("/_calls")
        calls = await calls_resp.json()
        assert len(calls) == 1
        assert calls[0]["name"] == "HassTurnOff"
        assert calls[0]["arguments"] == {"name": "kitchen"}

    async def test_tools_call_unknown_tool_rejected_and_unrecorded(self):
        await self.client.post("/_reset")

        resp = await self.client.post("/", json={
            "jsonrpc": "2.0", "id": 4,
            "method": "tools/call",
            "params": {"name": "HassTurnNope", "arguments": {}},
        })
        body = await resp.json()
        assert body["error"]["code"] == -32602
        assert "HassTurnNope" in body["error"]["message"]

        calls_resp = await self.client.get("/_calls")
        calls = await calls_resp.json()
        assert calls == []

    async def test_live_context_records_facade_stripped_arguments(self):
        await self.client.post("/_reset")

        response = await self.client.post("/", json={
            "jsonrpc": "2.0", "id": 5,
            "method": "tools/call",
            "params": {"name": "GetLiveContext", "arguments": {}},
        })
        assert "result" in await response.json()

        calls_response = await self.client.get("/_calls")
        assert await calls_response.json() == [{
            "name": "GetLiveContext",
            "arguments": {},
        }]
