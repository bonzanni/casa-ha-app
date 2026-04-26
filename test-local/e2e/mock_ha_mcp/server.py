"""Minimal Home Assistant MCP server mock for Casa e2e tests.

Implements just enough of the MCP JSON-RPC 2.0 protocol for Casa's
homeassistant client to:
- initialize (returns serverInfo)
- list tools (HassTurnOn / HassTurnOff / GetLiveContext)
- call tools (records {name, arguments} for assertion; returns canned ok)

Test side-channels (NOT MCP):
- POST /_reset → clear recorded calls
- GET /_calls → return recorded calls as JSON array
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

from aiohttp import web

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "HassTurnOn",
        "description": "Turn on a Home Assistant entity by name or area.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "area": {"type": "string"},
            },
        },
    },
    {
        "name": "HassTurnOff",
        "description": "Turn off a Home Assistant entity by name or area.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "area": {"type": "string"},
            },
        },
    },
    {
        "name": "GetLiveContext",
        "description": "Return current state of all exposed Home Assistant entities.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# In-memory state. Cleared via POST /_reset.
STATE: dict[str, Any] = {"calls": []}


def _ok(req_id: Any, result: Any) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, message: str) -> web.Response:
    return web.json_response({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    })


async def handle_jsonrpc(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return _error(None, -32700, "parse error")

    req_id = payload.get("id")
    method = payload.get("method")

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": "homeassistant-mock", "version": "0.1.0"},
            "capabilities": {"tools": {}},
        })

    if method == "tools/list":
        return _ok(req_id, {"tools": TOOLS})

    if method == "tools/call":
        params = payload.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        known = {t["name"] for t in TOOLS}
        if name not in known:
            # Reject unknown tool names so misspellings in e2e fail loudly
            # instead of silently appending a phantom call.
            return _error(req_id, -32602, f"unknown tool: {name!r}")
        STATE["calls"].append({"name": name, "arguments": arguments})

        if name == "GetLiveContext":
            text = json.dumps({
                "lights.kitchen": "on",
                "lights.bedroom": "off",
                "climate.living_room": {"temperature": 21.0, "target": 22.0},
            })
        else:
            text = json.dumps({"success": True, "tool": name})
        return _ok(req_id, {"content": [{"type": "text", "text": text}]})

    return _error(req_id, -32601, f"method not found: {method}")


async def handle_calls(_request: web.Request) -> web.Response:
    return web.json_response(STATE["calls"])


async def handle_reset(_request: web.Request) -> web.Response:
    STATE["calls"] = []
    return web.json_response({"ok": True})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/", handle_jsonrpc)
    app.router.add_get("/_calls", handle_calls)
    app.router.add_post("/_reset", handle_reset)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOCK_HA_PORT", "8200")))
    args = parser.parse_args()

    web.run_app(build_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
