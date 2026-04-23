"""Throwaway aiohttp MCP JSON-RPC server that simulates a mid-tools/call blip.

Behavior:
  - /mcp/casa-framework-spike/initialize → normal response
  - /mcp/casa-framework-spike/tools/list → advertises a single tool: "ping_cc"
  - /mcp/casa-framework-spike (tools/call, first time per id) → sleep 2s, close
  - /mcp/casa-framework-spike (tools/call, second time same id) → log "retry
    observed" and return success

If we see the same req_id twice, the CC client retries on close. If we never
see a retry before the test window closes, the CC client is optimistic.
"""

from __future__ import annotations

import asyncio
import json
import sys

from aiohttp import web

_seen: set[str] = set()
_retry_log: list[str] = []


async def handler(request: web.Request) -> web.Response:
    try:
        msg = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    method = msg.get("method")
    req_id = str(msg.get("id", ""))

    if method == "initialize":
        return web.json_response({
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mcp-blip-spike", "version": "0.0.0"},
            },
        })

    if method == "notifications/initialized":
        return web.Response(status=202)

    if method == "tools/list":
        return web.json_response({
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"tools": [{
                "name": "ping_cc",
                "description": "Returns pong",
                "inputSchema": {"type": "object", "properties": {}},
            }]},
        })

    if method == "tools/call":
        if req_id in _seen:
            _retry_log.append(req_id)
            print(f"RETRY OBSERVED for req_id={req_id}", flush=True)
            return web.json_response({
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "result": {"content": [{"type": "text", "text": "pong"}]},
            })
        _seen.add(req_id)
        await asyncio.sleep(2)
        # Abruptly close — simulates svc-casa dying mid-call.
        raise ConnectionResetError("simulated mid-call close")

    return web.json_response({
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "error": {"code": -32601, "message": f"unknown method {method}"},
    })


def main() -> int:
    app = web.Application()
    app.router.add_post("/mcp/casa-framework-spike", handler)
    web.run_app(app, host="127.0.0.1", port=8099, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
