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
    """Hook fail-closed on transport error: deny with actionable reason (F-1)."""
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
        reason = body["hookSpecificOutput"]["permissionDecisionReason"]
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        # F-1: reason must signal failure unambiguously so the model
        # doesn't narrate hook errors as success.
        assert "permission relay" in reason.lower()
        assert "tool was not run" in reason.lower()
        assert "casa" in reason.lower()


async def test_svc_hooks_resolve_forwards_with_no_client_timeout() -> None:
    """E-1: /hooks/resolve must defer to casa-main's policy-driven timeout.

    The svc-layer forwarder must call ``forward_to_internal`` with
    ``timeout_s=None`` so a slow operator-in-the-loop relay (e.g.
    ``engagement_permission_relay`` with policy ``timeout: 600``) is not
    truncated by a hardcoded 10s client-side timeout.
    """
    captured: dict = {}

    async def _fwd(**kwargs):
        captured.update(kwargs)
        return 200, {"hookSpecificOutput": {"permissionDecision": "allow"}}
    app = _make_svc_app(tools=[], forward_call=_fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={
                "policy": "engagement_permission_relay",
                "payload": {"tool_name": "Bash"},
            },
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "allow"
    # E-1 contract: forwarder timeout disabled in the hooks path.
    assert "timeout_s" in captured, (
        "/hooks/resolve must pass timeout_s explicitly to defer to casa-main"
    )
    assert captured["timeout_s"] is None, (
        f"E-1 regression: expected timeout_s=None, got {captured['timeout_s']!r}"
    )


async def test_svc_hooks_resolve_slow_forwarder_does_not_time_out() -> None:
    """E-1: a forwarder that takes longer than the old 10s default must
    still complete successfully — the policy-driven timeout on casa-main
    is the only effective gate."""
    import asyncio
    started = asyncio.get_event_loop().time()

    async def _slow_fwd(**_):
        # 0.2s — well under any reasonable test timeout, but proves we
        # don't depend on the legacy 10s default. The contract assertion
        # (timeout_s=None) is in the companion test above.
        await asyncio.sleep(0.2)
        return 200, {"hookSpecificOutput": {"permissionDecision": "allow"}}
    app = _make_svc_app(tools=[], forward_call=_slow_fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "x", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
    elapsed = asyncio.get_event_loop().time() - started
    assert body["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert elapsed >= 0.2  # forwarder actually slept; not bypassed


async def test_svc_hooks_resolve_forwarder_error_reason_is_actionable() -> None:
    """F-1: forwarder error (timeout / ClientError) must produce an
    actionable deny reason — the previous empty 'hook forward error: '
    confused the model into narrating success.

    NOTE: with E-1 (timeout_s=None) shipped, the asyncio.TimeoutError
    branch will only fire if casa-main itself returns/closes within its
    own deadline; this test still pins the message format for any
    aiohttp.ClientError that might surface (e.g. payload, response,
    connection-reset).
    """
    import aiohttp

    async def _fwd_raises(**_):
        raise aiohttp.ClientPayloadError("simulated payload error")
    app = _make_svc_app(tools=[], forward_call=_fwd_raises)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "anything", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        reason = body["hookSpecificOutput"]["permissionDecisionReason"]
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "permission relay failed" in reason.lower()
        assert "tool was not run" in reason.lower()
        # Exception class name MUST leak for operator debugging — the old
        # path produced an empty "hook forward error: " on TimeoutError.
        assert "ClientPayloadError" in reason


async def test_svc_tools_call_keeps_default_short_timeout() -> None:
    """E-1 scope guard: the tools/call route must NOT inherit the hooks
    path's timeout_s=None. Model-driven tool calls have no human in the
    loop, so the short default is correct there."""
    captured: dict = {}

    async def _fwd(**kwargs):
        captured.update(kwargs)
        return 200, {"content": [{"type": "text", "text": "ok"}]}
    app = _make_svc_app(tools=[_DummyTool()], forward_call=_fwd)
    async with TestClient(TestServer(app)) as client:
        await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "ok", "arguments": {}},
            },
        )
    # tools/call must NOT explicitly set timeout_s (defer to the
    # forwarder's default 10s).
    assert "timeout_s" not in captured, (
        "tools/call should rely on the default forwarder timeout, "
        f"got explicit timeout_s={captured.get('timeout_s')!r}"
    )


async def test_svc_get_returns_405() -> None:
    fwd = AsyncMock()
    app = _make_svc_app(tools=[], forward_call=fwd)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/mcp/casa-framework")
        assert resp.status == 405
