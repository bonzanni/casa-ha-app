"""JSON-RPC 2.0 HTTP bridge for casa-framework MCP tools.

See docs/superpowers/specs/2026-04-23-3.5-plan4a-1-mcp-bridge-design.md §4.

This bridge is stateless: every POST is a self-contained JSON-RPC envelope.
No session state, no SSE. GET returns 405.

Engagement identity propagates via the `X-Casa-Engagement-Id` request header,
written into `.mcp.json` by `drivers.workspace.provision_workspace`. The
dispatcher binds `tools.engagement_var` from that header for the duration
of the tool coroutine and resets it in `finally`, so tools that gate on
`engagement_var.get(None)` (emit_completion, query_engager) work the same
on the HTTP path as on the SDK path.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiohttp import web

# NOTE: Any is an internal type in `claude_agent_sdk`; neither the pinned
# 0.1.61 release nor the e2e mock SDK re-export it. We rely only on duck-typing
# via ``.name``, ``.description``, ``.input_schema``, ``.handler`` — no runtime
# import is needed. Type hints use ``Any`` to keep mypy/pylance happy without
# pinning a private symbol.

logger = logging.getLogger(__name__)

# Envelope helpers + version constants extracted to mcp_envelope.py in v0.14.0
# (Plan 4b Phase 3.6). mcp_bridge.py re-exports them transitionally; the
# whole module is deleted in Milestone 8 of the v0.14.0 plan.
from mcp_envelope import (  # noqa: F401 — re-exported for back-compat
    PROTOCOL_VERSION,
    VERSION,
    _jsonrpc_error,
    _jsonrpc_ok,
    _py_type_to_json,
    _tool_schema,
)


# ---------------------------------------------------------------------------
# tools/call dispatch
# ---------------------------------------------------------------------------


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _build_tool_dispatch(
    tools: tuple[Any, ...],
) -> dict[str, ToolHandler]:
    """Return {tool_name: handler_coroutine} from the shared CASA_TOOLS tuple."""
    return {t.name: t.handler for t in tools}


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


async def _mcp_get_not_allowed(_request: web.Request) -> web.Response:
    """GET /mcp/casa-framework is not supported — we're a stateless POST-only server."""
    return web.Response(status=405, text="Method Not Allowed\n",
                        headers={"Allow": "POST"})


def _make_mcp_handler(
    *,
    tools: tuple[Any, ...],
    engagement_registry: Any,
):
    """Build the aiohttp POST handler for /mcp/casa-framework.

    tools is the shared CASA_TOOLS tuple.
    engagement_registry is used to look up an EngagementRecord from the
    X-Casa-Engagement-Id header before dispatching tools/call.
    """
    dispatch = _build_tool_dispatch(tools)
    tool_schemas = [_tool_schema(t) for t in tools]

    async def handler(request: web.Request) -> web.Response:
        try:
            msg = await request.json()
        except Exception:
            return _jsonrpc_error(None, -32700, "Parse error")

        if not isinstance(msg, dict):
            return _jsonrpc_error(None, -32600, "Invalid Request")

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "notifications/initialized":
            # JSON-RPC notification: no response body, HTTP 202.
            return web.Response(status=202)

        if method == "initialize":
            return _jsonrpc_ok(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "casa-framework", "version": VERSION},
            })

        if method == "tools/list":
            return _jsonrpc_ok(req_id, {"tools": tool_schemas})

        if method == "tools/call":
            return await _dispatch_tool_call(
                req_id=req_id,
                params=params,
                dispatch=dispatch,
                engagement_registry=engagement_registry,
                headers=request.headers,
            )

        if method == "ping":
            return _jsonrpc_ok(req_id, {})

        return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    return handler


# ---------------------------------------------------------------------------
# tools/call dispatcher with engagement context binding
# ---------------------------------------------------------------------------


async def _dispatch_tool_call(
    *,
    req_id: Any,
    params: dict[str, Any],
    dispatch: dict[str, ToolHandler],
    engagement_registry: Any,
    headers,
) -> web.Response:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(name, str):
        return _jsonrpc_error(req_id, -32602, "missing params.name")

    fn = dispatch.get(name)
    if fn is None:
        return _jsonrpc_error(req_id, -32602, f"Unknown tool: {name}")

    # Look up engagement from header; if missing, invalid, or finalized,
    # bind None — the tool's own guards (emit_completion / query_engager)
    # return not_in_engagement in that case.
    eng_id = headers.get("X-Casa-Engagement-Id")
    engagement = None
    if eng_id:
        try:
            rec = engagement_registry.get(eng_id)
        except Exception:  # noqa: BLE001
            rec = None
        # Only bind if engagement is still UNDERGOING (defense-in-depth).
        if rec is not None and getattr(rec, "status", None) == "active":
            engagement = rec

    # Import engagement_var lazily so tests that monkeypatch the tools module
    # see the up-to-date ContextVar symbol.
    from tools import engagement_var

    token = engagement_var.set(engagement)
    try:
        result = await fn(arguments)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "MCP bridge: tool %r raised: %s", name, exc,
        )
        return _jsonrpc_error(req_id, -32000, f"Tool {name!r} raised: {exc}")
    finally:
        engagement_var.reset(token)

    # Tool return shape is already {"content": [...]} per the @tool(...)
    # decorator convention — wrap unchanged.
    return _jsonrpc_ok(req_id, result)
