"""Tests for the /hooks/resolve loopback endpoint (Plan 4a.1 real-path).

The handler calls the real async HookCallback from HOOK_POLICIES[name]["factory"]
and returns whatever the callback returns:
  - None from the callback → HTTP 200 empty object {} (CC treats this as allow).
  - dict from the callback (already CC-native {"hookSpecificOutput": {...}}) → pass through.
  - Unknown policy or malformed payload → 200 with a deny dict.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio


async def test_unknown_policy_returns_deny_body():
    from internal_handlers import _make_internal_hooks_resolve_handler as _make_hooks_resolve_handler

    handler = _make_hooks_resolve_handler(hook_policies={})
    app = web.Application()
    app.router.add_post("/hooks/resolve", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "nope", "payload": {"tool_name": "Bash"}},
        )
        assert resp.status == 200
        body = await resp.json()
        out = body.get("hookSpecificOutput") or {}
        assert out.get("permissionDecision") == "deny"
        assert "unknown policy" in (out.get("permissionDecisionReason") or "").lower()


async def test_callback_returning_none_returns_empty_allow():
    """HookCallback returning None → HTTP 200 with {} (CC interprets as allow)."""
    from internal_handlers import _make_internal_hooks_resolve_handler as _make_hooks_resolve_handler

    async def always_allow_callback(input_data, tool_use_id, context):
        return None

    handler = _make_hooks_resolve_handler(hook_policies={
        "my_policy": ("Bash", always_allow_callback),
    })
    app = web.Application()
    app.router.add_post("/hooks/resolve", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "my_policy", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body == {}


async def test_callback_returning_deny_is_passed_through():
    from internal_handlers import _make_internal_hooks_resolve_handler as _make_hooks_resolve_handler

    async def deny_cb(input_data, tool_use_id, context):
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "test-blocked",
        }}

    handler = _make_hooks_resolve_handler(hook_policies={
        "deny_all": ("Write|Edit", deny_cb),
    })
    app = web.Application()
    app.router.add_post("/hooks/resolve", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "deny_all", "payload": {"tool_name": "Write"}},
        )
        body = await resp.json()
        out = body["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert out["permissionDecisionReason"] == "test-blocked"


async def test_matcher_mismatch_returns_empty_allow():
    """When payload.tool_name does not match the policy's matcher regex,
    the handler returns {} without calling the callback."""
    from internal_handlers import _make_internal_hooks_resolve_handler as _make_hooks_resolve_handler

    called = {"n": 0}
    async def cb(input_data, tool_use_id, context):
        called["n"] += 1
        return None

    handler = _make_hooks_resolve_handler(hook_policies={
        "write_only": ("Write|Edit", cb),
    })
    app = web.Application()
    app.router.add_post("/hooks/resolve", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "write_only", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body == {}
        assert called["n"] == 0  # matcher gated the call


async def test_malformed_json_returns_deny():
    from internal_handlers import _make_internal_hooks_resolve_handler as _make_hooks_resolve_handler

    handler = _make_hooks_resolve_handler(hook_policies={})
    app = web.Application()
    app.router.add_post("/hooks/resolve", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/hooks/resolve",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        body = await resp.json()
        out = body["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"


async def test_callback_exception_returns_deny():
    from internal_handlers import _make_internal_hooks_resolve_handler as _make_hooks_resolve_handler

    async def boom(input_data, tool_use_id, context):
        raise RuntimeError("policy kapow")

    handler = _make_hooks_resolve_handler(hook_policies={
        "boom_policy": ("Bash", boom),
    })
    app = web.Application()
    app.router.add_post("/hooks/resolve", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "boom_policy", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        out = body["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "kapow" in out["permissionDecisionReason"]


async def test_build_cc_hook_policies_builds_real_tuples():
    """_build_cc_hook_policies must return {name: (matcher, callback)} with
    real async callbacks, not stubs."""
    from casa_core import _build_cc_hook_policies
    from hooks import HOOK_POLICIES

    cc = _build_cc_hook_policies(HOOK_POLICIES)
    assert "casa_config_guard" in cc
    matcher, callback = cc["casa_config_guard"]
    assert matcher == "Write|Edit|Bash"
    import inspect
    assert inspect.iscoroutinefunction(callback), (
        "callback must be async — stub wrappers are gone in v0.13.1"
    )
