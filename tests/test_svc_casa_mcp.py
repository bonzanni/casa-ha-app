# tests/test_svc_casa_mcp.py
"""Unit tests for svc_casa_mcp.py — the standalone MCP service.

Strategy: import the module's helpers (envelope dispatch, forwarder)
and test each independently. No real Unix socket — we mock the
aiohttp ClientSession to assert request shape and inject responses.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="svc_casa_mcp tests use UnixConnector path (Linux only)",
    ),
]


class _DummyTool:
    name = "ok"
    description = "Test"
    input_schema = {"x": int}
    handler = None


def _make_svc_app(tools: list, forward_call) -> web.Application:
    """Build the svc app with a mocked Unix-socket forwarder."""
    from svc_casa_mcp import _build_app
    return _build_app(tools=tools, forward_to_internal=forward_call)


async def test_svc_initialize() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[_DummyTool()], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
        })
        body = await resp.json()
        assert body["result"]["serverInfo"]["name"] == "casa-framework"
        assert body["result"]["protocolVersion"] == "2025-06-18"
        # Initialize doesn't touch the forwarder.
        fwd.assert_not_called()


async def test_svc_tools_list_uses_static_snapshot() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[_DummyTool()], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        })
        body = await resp.json()
        names = [t["name"] for t in body["result"]["tools"]]
        assert names == ["ok"]
        fwd.assert_not_called()


async def test_svc_notifications_initialized_returns_202() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        assert resp.status == 202
        fwd.assert_not_called()


async def test_svc_ping_returns_empty_result() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "id": 99, "method": "ping",
        })
        body = await resp.json()
        assert body == {"jsonrpc": "2.0", "id": 99, "result": {}}


async def test_svc_unknown_method_returns_jsonrpc_error() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "id": 7, "method": "no_such",
        })
        body = await resp.json()
        assert body["error"]["code"] == -32601


async def test_svc_tools_call_forwards_to_internal_with_engagement_id() -> None:
    fwd = AsyncMock(return_value=(
        200, {"content": [{"type": "text", "text": "ok"}]},
    ))
    app = _make_svc_app(tools=[_DummyTool()], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "ok", "arguments": {"x": 1}},
            },
            headers={"X-Casa-Engagement-Id": "eng-9"},
        )
        body = await resp.json()
        assert body["result"] == {"content": [{"type": "text", "text": "ok"}]}
        # forward_call called with internal-shape body.
        call_args = fwd.call_args
        assert call_args.kwargs["path"] == "/internal/tools/call"
        assert call_args.kwargs["body"] == {
            "name": "ok",
            "arguments": {"x": 1},
            "engagement_id": "eng-9",
        }


async def test_svc_tools_call_internal_returns_error_object_passes_through_as_jsonrpc_error() -> None:
    fwd = AsyncMock(return_value=(
        200, {"error": {"code": -32001, "message": "Tool 'ok' raised: boom"}},
    ))
    app = _make_svc_app(tools=[_DummyTool()], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "ok", "arguments": {}},
            },
        )
        body = await resp.json()
        assert body["error"]["code"] == -32001
        assert "boom" in body["error"]["message"]


async def test_svc_tools_call_socket_unreachable_returns_casa_unavailable() -> None:
    """ClientConnectorError → -32000 casa_temporarily_unavailable."""
    import aiohttp

    async def _fwd_raises(**_):
        # Simulate Unix socket missing / refused.
        raise aiohttp.ClientConnectorError(
            connection_key=MagicMock(),
            os_error=ConnectionRefusedError("simulated"),
        )
    app = _make_svc_app(tools=[_DummyTool()], forward_call=_fwd_raises)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "ok", "arguments": {}},
            },
        )
        body = await resp.json()
        assert body["error"]["code"] == -32000
        assert "casa" in body["error"]["message"].lower()


async def test_svc_hooks_resolve_forwards_body_to_internal() -> None:
    fwd = AsyncMock(return_value=(
        200, {"hookSpecificOutput": {"permissionDecision": "allow"}},
    ))
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "allow_all", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "allow"
        # forward_call received the body as-is.
        call_args = fwd.call_args
        assert call_args.kwargs["path"] == "/internal/hooks/resolve"
        assert call_args.kwargs["body"] == {
            "policy": "allow_all", "payload": {"tool_name": "Bash"},
        }


async def test_svc_hooks_resolve_socket_unreachable_fails_closed() -> None:
    """Hook fail-closed on transport error: deny."""
    import aiohttp

    async def _fwd_raises(**_):
        raise aiohttp.ClientConnectorError(
            connection_key=MagicMock(),
            os_error=ConnectionRefusedError("simulated"),
        )
    app = _make_svc_app(tools=[], forward_call=_fwd_raises)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "anything", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "casa" in body["hookSpecificOutput"]["permissionDecisionReason"].lower()


async def test_svc_get_returns_405() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/mcp/casa-framework")
        assert resp.status == 405
