"""Unit + integration tests for the MCP JSON-RPC 2.0 HTTP bridge."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio


# --- 4.1 — JSON-RPC envelope helpers --------------------------------------


def test_jsonrpc_ok_envelope_shape():
    from mcp_bridge import _jsonrpc_ok

    resp = _jsonrpc_ok(req_id=42, result={"foo": "bar"})
    # aiohttp.web.json_response is what the helper returns; body access is
    # via resp._body (private but stable across aiohttp versions we use).
    import json
    body = json.loads(resp.body)
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 42
    assert body["result"] == {"foo": "bar"}
    assert "error" not in body


def test_jsonrpc_error_envelope_shape():
    from mcp_bridge import _jsonrpc_error

    resp = _jsonrpc_error(req_id=7, code=-32601, message="Method not found: ping")
    import json
    body = json.loads(resp.body)
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    assert body["error"]["code"] == -32601
    assert "Method not found" in body["error"]["message"]
    assert "result" not in body


def test_jsonrpc_error_id_none_for_parse_errors():
    """Parse errors carry id=None per JSON-RPC 2.0 §5.1."""
    from mcp_bridge import _jsonrpc_error

    resp = _jsonrpc_error(req_id=None, code=-32700, message="Parse error")
    import json
    body = json.loads(resp.body)
    assert body["id"] is None


# --- 4.2 — tool schema translation ----------------------------------------


async def test_tool_schema_includes_name_description_inputSchema():
    from mcp_bridge import _tool_schema
    from tools import emit_completion  # SdkMcpTool

    schema = _tool_schema(emit_completion)
    assert schema["name"] == "emit_completion"
    assert "description" in schema and schema["description"]
    assert "inputSchema" in schema
    ischema = schema["inputSchema"]
    assert ischema["type"] == "object"
    assert "properties" in ischema
    # emit_completion takes text/artifacts/next_steps/status
    assert "text" in ischema["properties"]
    # Casa tool schemas are dict-of-type style; the text param is str.
    assert ischema["properties"]["text"]["type"] == "string"


# --- 4.3 — dispatch table -------------------------------------------------


def test_build_tool_dispatch_maps_name_to_handler():
    from mcp_bridge import _build_tool_dispatch
    from tools import CASA_TOOLS

    dispatch = _build_tool_dispatch(CASA_TOOLS)
    assert "emit_completion" in dispatch
    assert "send_message" in dispatch
    # Values must be awaitable-callable (the raw .handler coroutine).
    import inspect
    for name, fn in dispatch.items():
        assert inspect.iscoroutinefunction(fn), f"{name}: handler must be async"


# --- 4.4 — full handler (initialize / notifications / tools/list / ping) --


async def _build_app(tools=None):
    """Build a test aiohttp app with the MCP bridge wired in."""
    from mcp_bridge import _make_mcp_handler, _mcp_get_not_allowed

    from tools import CASA_TOOLS
    tools = tools if tools is not None else CASA_TOOLS

    class _FakeRegistry:
        def get(self, eng_id):
            return None

    handler = _make_mcp_handler(
        tools=tools,
        engagement_registry=_FakeRegistry(),
    )

    app = web.Application()
    app.router.add_post("/mcp/casa-framework", handler)
    app.router.add_get("/mcp/casa-framework", _mcp_get_not_allowed)
    return app


async def test_initialize_returns_server_info():
    app = await _build_app()

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["id"] == 1
        result = body["result"]
        assert result["protocolVersion"] == "2025-06-18"
        assert result["serverInfo"]["name"] == "casa-framework"
        assert result["capabilities"] == {"tools": {}}


async def test_notifications_initialized_returns_202_no_body():
    app = await _build_app()

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert resp.status == 202
        text = await resp.text()
        assert text == ""


async def test_tools_list_returns_all_CASA_TOOLS():
    from tools import CASA_TOOLS
    app = await _build_app()

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        body = await resp.json()
        names = {t["name"] for t in body["result"]["tools"]}
        expected = {t.name for t in CASA_TOOLS}
        assert names == expected


async def test_ping_returns_empty_result():
    app = await _build_app()
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={"jsonrpc": "2.0", "id": 3, "method": "ping"},
        )
        body = await resp.json()
        assert body["result"] == {}


async def test_unknown_method_returns_negative_32601():
    app = await _build_app()
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={"jsonrpc": "2.0", "id": 4, "method": "nonexistent/thing"},
        )
        body = await resp.json()
        assert body["error"]["code"] == -32601
        assert "Method not found" in body["error"]["message"]


async def test_malformed_body_returns_parse_error():
    app = await _build_app()
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            data="not json at all",
            headers={"Content-Type": "application/json"},
        )
        body = await resp.json()
        assert body["error"]["code"] == -32700
        assert body["id"] is None


async def test_get_returns_405():
    app = await _build_app()
    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.get("/mcp/casa-framework")
        assert resp.status == 405


# --- 4.5 — tools/call dispatch + engagement context binding ---------------


class _FakeEngagement:
    """Minimal stand-in for EngagementRecord — handler treats it as opaque."""
    def __init__(self, eid: str, status: str = "active"):
        self.id = eid
        self.status = status


class _FakeEngagementRegistry:
    def __init__(self, records: dict):
        self._records = records
    def get(self, eng_id):
        return self._records.get(eng_id)


async def test_tools_call_unknown_tool_returns_invalid_params():
    from mcp_bridge import _make_mcp_handler, _mcp_get_not_allowed
    from tools import CASA_TOOLS

    handler = _make_mcp_handler(
        tools=CASA_TOOLS,
        engagement_registry=_FakeEngagementRegistry({}),
    )
    app = web.Application()
    app.router.add_post("/mcp/casa-framework", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "does_not_exist", "arguments": {}},
            },
        )
        body = await resp.json()
        assert body["error"]["code"] == -32602
        assert "does_not_exist" in body["error"]["message"]


async def test_tools_call_dispatches_to_handler_and_wraps_result():
    """A synthetic tool should receive arguments and its return wraps into result."""
    from mcp_bridge import _make_mcp_handler
    from claude_agent_sdk import tool

    @tool("echo_test", "test echo", {"msg": str})
    async def echo_test(args: dict) -> dict:
        return {"content": [{"type": "text", "text": args.get("msg", "")}]}

    handler = _make_mcp_handler(
        tools=(echo_test,),
        engagement_registry=_FakeEngagementRegistry({}),
    )
    app = web.Application()
    app.router.add_post("/mcp/casa-framework", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "echo_test", "arguments": {"msg": "hi"}},
            },
        )
        body = await resp.json()
        assert body["result"]["content"][0]["text"] == "hi"


async def test_engagement_header_binds_context_var():
    """A tool that reads engagement_var.get(None) sees the registered record
    when X-Casa-Engagement-Id is set."""
    from mcp_bridge import _make_mcp_handler
    from claude_agent_sdk import tool

    seen = {}

    @tool("read_eng", "read engagement var", {})
    async def read_eng(_args: dict) -> dict:
        from tools import engagement_var
        seen["eng"] = engagement_var.get(None)
        return {"content": [{"type": "text", "text": "ok"}]}

    fake = _FakeEngagement("abc1")
    handler = _make_mcp_handler(
        tools=(read_eng,),
        engagement_registry=_FakeEngagementRegistry({"abc1": fake}),
    )
    app = web.Application()
    app.router.add_post("/mcp/casa-framework", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "read_eng", "arguments": {}},
            },
            headers={"X-Casa-Engagement-Id": "abc1"},
        )
        assert resp.status == 200
        assert seen["eng"] is fake


async def test_engagement_header_missing_binds_none():
    from mcp_bridge import _make_mcp_handler
    from claude_agent_sdk import tool

    seen = {}
    @tool("read_eng2", "read engagement var 2", {})
    async def read_eng2(_args: dict) -> dict:
        from tools import engagement_var
        seen["eng"] = engagement_var.get(None)
        return {"content": [{"type": "text", "text": "ok"}]}

    handler = _make_mcp_handler(
        tools=(read_eng2,),
        engagement_registry=_FakeEngagementRegistry({}),
    )
    app = web.Application()
    app.router.add_post("/mcp/casa-framework", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "read_eng2", "arguments": {}},
            },
        )
        assert seen["eng"] is None


async def test_engagement_var_reset_after_call():
    """engagement_var must be reset even if the tool raises."""
    from mcp_bridge import _make_mcp_handler
    from claude_agent_sdk import tool

    @tool("boom", "raises", {})
    async def boom(_args: dict) -> dict:
        raise RuntimeError("kapow")

    fake = _FakeEngagement("abc2")
    handler = _make_mcp_handler(
        tools=(boom,),
        engagement_registry=_FakeEngagementRegistry({"abc2": fake}),
    )
    app = web.Application()
    app.router.add_post("/mcp/casa-framework", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": "boom", "arguments": {}},
            },
            headers={"X-Casa-Engagement-Id": "abc2"},
        )
        body = await resp.json()
        assert body["error"]["code"] == -32000
        assert "kapow" in body["error"]["message"]

    # Confirm the ContextVar is back to None in the test process.
    from tools import engagement_var
    assert engagement_var.get(None) is None


async def test_emit_completion_via_bridge_end_to_end(monkeypatch):
    """emit_completion called via the HTTP bridge with a valid engagement
    header transitions the registry record to completed."""
    from mcp_bridge import _make_mcp_handler
    import tools as tools_mod
    from tools import CASA_TOOLS
    from engagement_registry import EngagementRegistry, EngagementRecord

    reg = EngagementRegistry(tombstone_path="/tmp/x-none.json", bus=None)
    rec = EngagementRecord(
        id="eng_bridge",
        kind="executor", role_or_type="hello-driver", driver="claude_code",
        status="active", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )
    reg._records[rec.id] = rec
    # Wire _engagement_registry so _finalize_engagement updates it.
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_channel_manager", None)
    monkeypatch.setattr(tools_mod, "_bus", None)

    handler = _make_mcp_handler(
        tools=CASA_TOOLS, engagement_registry=reg,
    )
    app = web.Application()
    app.router.add_post("/mcp/casa-framework", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/mcp/casa-framework",
            json={
                "jsonrpc": "2.0", "id": 11, "method": "tools/call",
                "params": {
                    "name": "emit_completion",
                    "arguments": {"text": "done", "artifacts": [],
                                  "next_steps": [], "status": "ok"},
                },
            },
            headers={"X-Casa-Engagement-Id": "eng_bridge"},
        )
        body = await resp.json()
        assert "result" in body, f"unexpected: {body}"

    assert reg.get("eng_bridge").status == "completed"
