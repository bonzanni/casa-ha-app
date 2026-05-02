# casa-agent/rootfs/opt/casa/internal_handlers.py
"""Internal HTTP handlers -- bound to the casa-main Unix socket
(/run/casa/internal.sock) and consumed in-process by the public-8099
back-compat fallback.

Body shape (no JSON-RPC envelope, no header dependency):

    POST /internal/tools/call
    {
      "name": "<tool_name>",
      "arguments": {...},
      "engagement_id": "<uuid>" | null
    }

    POST /internal/hooks/resolve
    {
      "policy": "<policy_name>",
      "payload": {...}            # CC PreToolUse payload
    }

Responses are bare (no JSON-RPC wrapping):
- tools/call success: {"content": [...]}                          (tool's own shape)
- tools/call known error: {"error": {"code": -32xxx, "message": ...}}
- hooks/resolve allow: {}
- hooks/resolve deny:  {"hookSpecificOutput": {...}}              (CC-native)

The svc-casa-mcp service wraps tools/call results in JSON-RPC envelopes
(adds {"jsonrpc": "2.0", "id": ..., "result": ...} or .error), and on
ClientConnectorError to the Unix socket returns -32000 casa_temporarily_unavailable.
The hook responses are pass-through (CC's hook protocol is already body-only).
"""

from __future__ import annotations

import logging
import re as _re
from typing import Any, Awaitable, Callable

from aiohttp import web

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
HookCallback = Callable[[dict[str, Any], Any, dict], Awaitable[dict | None]]


# ---------------------------------------------------------------------------
# tools/call handler factory
# ---------------------------------------------------------------------------


def _make_internal_tools_call_handler(
    *,
    tool_dispatch: dict[str, ToolHandler],
    engagement_registry: Any,
):
    """Build the aiohttp POST handler for /internal/tools/call.

    `tool_dispatch` is a {name -> async-callable} map; in casa-main this is
    built from `tools.CASA_TOOLS` at startup and passed in. Tests inject
    a smaller fake.

    `engagement_registry` is used to look up records by id when the body
    carries `engagement_id`. Bound records with status == "active" populate
    `tools.engagement_var`; other states (or missing record) bind None.
    """
    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"code": -32700, "message": "Parse error"}}
            )

        if not isinstance(body, dict):
            return web.json_response(
                {"error": {"code": -32600, "message": "Invalid Request"}}
            )

        name = body.get("name")
        arguments = body.get("arguments") or {}
        eng_id = body.get("engagement_id")

        if not isinstance(name, str):
            return web.json_response(
                {"error": {"code": -32602, "message": "missing name"}}
            )

        fn = tool_dispatch.get(name)
        if fn is None:
            return web.json_response(
                {"error": {"code": -32602, "message": f"Unknown tool: {name}"}}
            )

        # Resolve engagement record. Defense-in-depth: only bind when status
        # is still active. Mirrors v0.13.1 mcp_bridge._dispatch_tool_call.
        engagement = None
        if eng_id:
            try:
                rec = engagement_registry.get(eng_id)
            except Exception:  # noqa: BLE001
                rec = None
            if rec is not None and getattr(rec, "status", None) == "active":
                engagement = rec

        # Lazy import so monkeypatching `tools.engagement_var` in tests works.
        from tools import engagement_var

        token = engagement_var.set(engagement)
        try:
            result = await fn(arguments)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "internal /tools/call: tool %r raised: %s", name, exc,
            )
            # Distinct code from -32000 (used by svc for socket-down).
            return web.json_response(
                {"error": {"code": -32001,
                           "message": f"Tool {name!r} raised: {exc}"}}
            )
        finally:
            engagement_var.reset(token)

        return web.json_response(result)

    return handler


# ---------------------------------------------------------------------------
# hooks/resolve handler factory
# ---------------------------------------------------------------------------


def _make_internal_hooks_resolve_handler(
    *,
    hook_policies: dict[str, tuple[str, HookCallback]],
):
    """Build the aiohttp POST handler for /internal/hooks/resolve.

    `hook_policies` is the {name -> (matcher_regex, async_callback)} dict
    produced by `casa_core._build_cc_hook_policies(HOOK_POLICIES)`.
    """
    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        "internal/hooks/resolve: malformed JSON",
                }},
            )

        policy_name = body.get("policy")
        payload = body.get("payload") or {}

        entry = hook_policies.get(policy_name)
        if entry is None:
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        f"unknown policy: {policy_name}",
                }},
            )
        matcher_regex, callback = entry

        tool_name = payload.get("tool_name", "")
        if not _re.fullmatch(matcher_regex, tool_name):
            return web.json_response({})  # empty = allow

        try:
            result = await callback(payload, None, {})
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        f"policy {policy_name!r} raised: {exc}",
                }},
            )

        if result is None:
            return web.json_response({})
        return web.json_response(result)

    return handler


# ---------------------------------------------------------------------------
# /admin/reload handler factory (Task E.1, granular-reload plan)
# ---------------------------------------------------------------------------


def build_admin_reload_handler(*, runtime):
    """Factory -- returns an aiohttp handler that dispatches reload calls.

    Used by the internal-socket aiohttp app (registered in
    ``casa_core.start_internal_unix_runner``). Operator CLI ``casactl``
    POSTs to ``/admin/reload`` over the unix socket; same dispatch path
    as the ``casa_reload(scope=...)`` MCP tool.

    If ``runtime`` is None at registration time, the handler falls back
    to ``agent.active_runtime`` at request time. This handles the case
    where the route is registered before ``casa_core.main`` has bound
    ``active_runtime`` (boot ordering).

    ``reload.dispatch`` is looked up per-request (not at factory time)
    so tests can monkeypatch ``reload.dispatch`` after the route has
    been registered.
    """
    async def handler(request: web.Request) -> web.Response:
        import reload as reload_mod
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "kind": "bad_json",
                 "message": "POST body must be JSON"},
                status=400,
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"status": "error", "kind": "bad_json",
                 "message": "POST body must be a JSON object"},
                status=400,
            )

        scope = (payload.get("scope") or "").strip()
        if not scope:
            return web.json_response(
                {"status": "error", "kind": "scope_required",
                 "message": "missing 'scope' field"},
                status=400,
            )
        role_raw = payload.get("role")
        role = role_raw.strip() if isinstance(role_raw, str) else None
        if role == "":
            role = None
        include_env = bool(payload.get("include_env", False))

        active = runtime
        if active is None:
            import agent as agent_mod
            active = getattr(agent_mod, "active_runtime", None)
        if active is None:
            return web.json_response(
                {"status": "error", "kind": "not_initialized",
                 "message": "CasaRuntime not bound"},
                status=500,
            )

        result = await reload_mod.dispatch(
            scope, runtime=active, role=role, include_env=include_env,
        )
        status_code = 200 if result.get("status") == "ok" else 500
        return web.json_response(result, status=status_code)

    return handler
