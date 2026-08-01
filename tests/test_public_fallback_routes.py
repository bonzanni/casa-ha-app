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
        # #335: per-engagement secret; the body must present it to bind.
        self.auth_token = f"tok-{id}"


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
    """#335: the id claim rides with the matching engagement token header."""
    async with TestClient(TestServer(_build_app())) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "ok", "arguments": {"x": 7}},
            },
            headers={"X-Casa-Engagement-Id": "eng-active",
                     "X-Casa-Engagement-Token": "tok-eng-active"},
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


async def test_public_mcp_non_object_params_returns_32602() -> None:
    """#380/#342: a truthy non-object ``params`` must earn a typed -32602,
    not an AttributeError → HTTP 500."""
    async with TestClient(TestServer(_build_app())) as client:
        # Terra r1-1: falsy non-objects ([], "", 0, false) must be refused
        # too, not `or {}`-coerced into a silent empty call.
        for bad in ("scalar", 42, ["list"], [], "", 0, False):
            resp = await client.post("/mcp/casa-framework", json={
                "jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": bad,
            })
            assert resp.status == 200
            body = await resp.json()
            assert body["error"]["code"] == -32602, f"params={bad!r}"
            assert body["id"] == 9


async def test_public_mcp_non_object_arguments_returns_32602() -> None:
    """#380: same gate one level down — ``arguments`` must be an object."""
    async with TestClient(TestServer(_build_app())) as client:
        for bad in ("not-an-object", [], "", 0, False):
            resp = await client.post("/mcp/casa-framework", json={
                "jsonrpc": "2.0", "id": 10, "method": "tools/call",
                "params": {"name": "ok", "arguments": bad},
            })
            assert resp.status == 200
            body = await resp.json()
            assert body["error"]["code"] == -32602, f"arguments={bad!r}"


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


# ---------------------------------------------------------------------------
# #366: on the public route (old workspaces with the baked 8099 URL) the
# engagement identity comes from the SAME header pair the MCP twin reads —
# never from body fields a caller could smuggle in.
# ---------------------------------------------------------------------------


def _build_hooks_auth_app(calls: list) -> web.Application:
    from casa_core import _make_public_hooks_fallback_handler

    async def _recording_cb(_p, _t, context):
        calls.append(context)
        return None

    reg = _FakeReg()
    reg.add(_FakeRec("e" * 32))
    app = web.Application()
    app.router.add_post(
        "/hooks/resolve",
        _make_public_hooks_fallback_handler(
            hook_policies={"p": ("Bash", _recording_cb)},
            engagement_registry=reg,
        ),
    )
    return app


async def test_public_hooks_resolve_headers_authenticate_identity() -> None:
    calls: list = []
    async with TestClient(TestServer(_build_hooks_auth_app(calls))) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "p", "payload": {"tool_name": "Bash"}},
            headers={"X-Casa-Engagement-Id": "e" * 32,
                     "X-Casa-Engagement-Token": "tok-" + "e" * 32},
        )
        assert await resp.json() == {}
    assert calls[0].get("casa_engagement_id") == "e" * 32


async def test_public_hooks_resolve_bad_token_denies() -> None:
    calls: list = []
    async with TestClient(TestServer(_build_hooks_auth_app(calls))) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "p", "payload": {"tool_name": "Bash"}},
            headers={"X-Casa-Engagement-Id": "e" * 32,
                     "X-Casa-Engagement-Token": "WRONG"},
        )
        body = await resp.json()
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "engagement_auth_failed" in (
        body["hookSpecificOutput"]["permissionDecisionReason"])
    assert calls == []


async def test_public_hooks_resolve_body_identity_ignored() -> None:
    """A forger POSTing the INTERNAL body shape (id+token as body fields) at
    the public route gets no identity — the public twin reads headers only."""
    calls: list = []
    async with TestClient(TestServer(_build_hooks_auth_app(calls))) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "p", "payload": {"tool_name": "Bash"},
                  "engagement_id": "e" * 32,
                  "engagement_token": "tok-" + "e" * 32},
        )
        assert await resp.json() == {}
    assert calls[0].get("casa_engagement_id") is None
