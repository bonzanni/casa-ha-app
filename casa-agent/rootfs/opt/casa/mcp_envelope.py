# casa-agent/rootfs/opt/casa/mcp_envelope.py
"""JSON-RPC 2.0 envelope helpers + tool schema translation.

Pure helpers — no casa-state imports. Consumed by:
- svc_casa_mcp.py (the standalone MCP service on 127.0.0.1:8100)
- casa_core.py (the public-8099 back-compat fallback handlers)

Extracted from mcp_bridge.py (v0.13.1) so both consumers can share it
without circular imports.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

VERSION = "0.14.0"
PROTOCOL_VERSION = "2025-06-18"


# ---------------------------------------------------------------------------
# JSON-RPC envelope helpers
# ---------------------------------------------------------------------------


def _jsonrpc_ok(req_id: Any, result: Any) -> web.Response:
    return web.json_response({
        "jsonrpc": "2.0",
        "id": req_id,
        "result": result,
    })


def _jsonrpc_error(req_id: Any, code: int, message: str) -> web.Response:
    return web.json_response({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    })


# ---------------------------------------------------------------------------
# Python-type → JSON Schema translation
# ---------------------------------------------------------------------------


_PY_TO_JSON_TYPE: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _py_type_to_json(py_type: Any) -> dict[str, Any]:
    """Convert a Python-type annotation into a JSON Schema type dict.

    The @tool(...) decorator in claude_agent_sdk accepts a dict-of-types
    style (e.g. {"message": str}); we translate those here. Unknown types
    fall back to a permissive empty schema.
    """
    if py_type in _PY_TO_JSON_TYPE:
        return {"type": _PY_TO_JSON_TYPE[py_type]}
    if py_type is list:
        return {"type": "array"}
    if py_type is dict:
        return {"type": "object"}
    return {}


def _tool_schema(tool: Any) -> dict[str, Any]:
    """Return the MCP tools/list entry for an @tool-decorated function.

    `tool` is expected to be an SdkMcpTool-shaped object with .name,
    .description, .input_schema attributes.
    """
    ischema: dict[str, Any]
    raw = tool.input_schema
    # The dict-of-types case: empty dict (no-arg tools — O-1) or every
    # value is a Python type. The pre-built JSON Schema case: at least
    # one value is itself a dict (e.g. {"type": "string"}). We use the
    # latter as the discriminator.
    if isinstance(raw, dict) and not any(
        isinstance(v, dict) for v in raw.values()
    ):
        # dict-of-types — translate. Empty dict yields properties={}, which
        # is the correct shape for no-arg tools (CC v2.1.119 rejects bare
        # `inputSchema: {}` because the `type` field is missing).
        props = {name: _py_type_to_json(ty) for name, ty in raw.items()}
        ischema = {"type": "object", "properties": props}
    elif isinstance(raw, dict):
        # Pre-built JSON Schema — passthrough.
        ischema = raw
    else:
        # Unknown shape (TypedDict object, etc.) — permissive default.
        ischema = {"type": "object"}
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": ischema,
    }
