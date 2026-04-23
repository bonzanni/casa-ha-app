# tests/test_public_fallback_routes.py
"""Unit tests for the public-8099 back-compat fallback handlers built
in casa_core.py (Plan 4b Phase 3.6).

These wrap the new internal_handlers in JSON-RPC envelope code (for
/mcp/casa-framework) and adapt the body-vs-{policy,payload} shape (for
/hooks/resolve). The result must be byte-identical to v0.13.1's behavior
so that pre-v0.14.0 workspaces continue to function.
"""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio


async def _ok_tool(args):
    return {"content": [{"type": "text", "text": json.dumps(args)}]}


class _FakeReg:
    def __init__(self):
        self._by_id = {}
    def add(self, rec): self._by_id[rec.id] = rec
    def get(self, _id): return self._by_id.get(_id)


class _FakeRec:
    def __init__(self, id, status="active"):
        self.id = id
        self.status = status


def _build_app() -> web.Application:
    """Build an app with the new public-fallback handlers wired."""
    from casa_core import (
        _make_public_mcp_fallback_handler,
        _make_public_hooks_fallback_handler,
        _make_public_mcp_get_405_handler,
    )
    reg = _FakeReg()
    reg.add(_FakeRec("eng-active"))
    app = web.Application()
    app["_reg"] = reg
    app.router.add_post(
        "/mcp/casa-framework",
        _make_public_mcp_fallback_handler(
            tools=[_DummyTool()],
            tool_dispatch={"ok": _ok_tool},
            engagement_registry=reg,
        ),
    )
    app.router.add_get(
        "/mcp/casa-framework",
        _make_public_mcp_get_405_handler(),
    )
    app.router.add_post(
        "/hooks/resolve",
        _make_public_hooks_fallback_handler(
            hook_policies={"allow_all": ("Bash", _allow_cb)},
        ),
    )
    return app


class _DummyTool:
    name = "ok"
    description = "Test tool"
    input_schema = {"x": int}
    handler = _ok_tool


async def _allow_cb(_p, _c, _o):
    return {"hookSpecificOutput": {"permissionDecision": "allow"}}


async def test_public_mcp_initialize() -> None:
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
        })
        body = await resp.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        assert body["result"]["serverInfo"]["name"] == "casa-framework"
        # PROTOCOL_VERSION matches mcp_envelope.py value.
        assert body["result"]["protocolVersion"] == "2025-06-18"


async def test_public_mcp_tools_list() -> None:
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        })
        body = await resp.json()
        names = [t["name"] for t in body["result"]["tools"]]
        assert names == ["ok"]


async def test_public_mcp_tools_call_known_tool() -> None:
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "ok", "arguments": {"x": 7}},
            },
            headers={"X-Casa-Engagement-Id": "eng-active"},
        )
        body = await resp.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 3
        result = body["result"]
        assert result == {"content": [{"type": "text", "text": '{"x": 7}'}]}


async def test_public_mcp_tools_call_unknown_tool_returns_jsonrpc_error() -> None:
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            },
        )
        body = await resp.json()
        assert body["error"]["code"] == -32602
        assert "Unknown tool: nope" in body["error"]["message"]


async def test_public_mcp_notifications_initialized_returns_202() -> None:
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.post("/mcp/casa-framework", json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        assert resp.status == 202


async def test_public_mcp_get_returns_405() -> None:
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.get("/mcp/casa-framework")
        assert resp.status == 405


async def test_public_hooks_resolve_known_policy() -> None:
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "allow_all", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "allow"


async def test_public_hooks_resolve_unknown_policy_denies() -> None:
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "ghost", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "unknown policy" in body["hookSpecificOutput"]["permissionDecisionReason"]
