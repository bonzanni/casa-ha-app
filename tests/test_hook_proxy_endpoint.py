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


# ---------------------------------------------------------------------------
# #366: engagement identity on the hook path is CREDENTIAL-authenticated;
# the payload's cwd is caller-supplied text and only ever cross-checked.
# ---------------------------------------------------------------------------

ENG_A = "a" * 32
ENG_B = "b" * 32


class _Rec:
    def __init__(self, role_or_type="ptype", auth_token="tok-A"):
        self.role_or_type = role_or_type
        self.auth_token = auth_token


class _Registry:
    def __init__(self, recs):
        self._recs = recs

    def get(self, eng_id):
        return self._recs.get(eng_id)


def _auth_app(*, default_cb, exec_cb, registry):
    from internal_handlers import _make_internal_hooks_resolve_handler
    handler = _make_internal_hooks_resolve_handler(
        hook_policies={"p": ("Bash", default_cb)},
        executor_hook_policies={"ptype": {"p": ("Bash", exec_cb)}},
        engagement_registry=registry,
    )
    app = web.Application()
    app.router.add_post("/hooks/resolve", handler)
    return app


def _recorder(calls, name):
    async def cb(input_data, tool_use_id, context):
        calls.append((name, context))
        return None
    return cb


class TestHooksResolveEngagementAuth:
    async def test_valid_credential_selects_that_executors_params(self):
        calls: list = []
        registry = _Registry({ENG_A: _Rec()})
        app = _auth_app(default_cb=_recorder(calls, "default"),
                        exec_cb=_recorder(calls, "exec"), registry=registry)
        async with TestServer(app) as srv, TestClient(srv) as client:
            resp = await client.post("/hooks/resolve", json={
                "policy": "p",
                "payload": {"tool_name": "Bash",
                            "cwd": f"/data/engagements/{ENG_A}"},
                "engagement_id": ENG_A, "engagement_token": "tok-A",
            })
            assert await resp.json() == {}
        assert [c[0] for c in calls] == ["exec"]

    async def test_known_id_with_bad_token_denies_without_callback(self):
        calls: list = []
        registry = _Registry({ENG_A: _Rec()})
        app = _auth_app(default_cb=_recorder(calls, "default"),
                        exec_cb=_recorder(calls, "exec"), registry=registry)
        async with TestServer(app) as srv, TestClient(srv) as client:
            resp = await client.post("/hooks/resolve", json={
                "policy": "p",
                "payload": {"tool_name": "Bash",
                            "cwd": f"/data/engagements/{ENG_A}"},
                "engagement_id": ENG_A, "engagement_token": "WRONG",
            })
            out = (await resp.json())["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "engagement_auth_failed" in out["permissionDecisionReason"]
        assert calls == []

    async def test_known_id_with_missing_token_denies_without_callback(self):
        calls: list = []
        registry = _Registry({ENG_A: _Rec()})
        app = _auth_app(default_cb=_recorder(calls, "default"),
                        exec_cb=_recorder(calls, "exec"), registry=registry)
        async with TestServer(app) as srv, TestClient(srv) as client:
            resp = await client.post("/hooks/resolve", json={
                "policy": "p",
                "payload": {"tool_name": "Bash"},
                "engagement_id": ENG_A, "engagement_token": None,
            })
            out = (await resp.json())["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert calls == []

    async def test_unknown_id_falls_back_to_default_policies(self):
        """Mirrors the tools/call contract: an id the registry does not know
        proceeds UNAUTHENTICATED (default callbacks, no identity threaded)."""
        calls: list = []
        registry = _Registry({})
        app = _auth_app(default_cb=_recorder(calls, "default"),
                        exec_cb=_recorder(calls, "exec"), registry=registry)
        async with TestServer(app) as srv, TestClient(srv) as client:
            resp = await client.post("/hooks/resolve", json={
                "policy": "p", "payload": {"tool_name": "Bash"},
                "engagement_id": ENG_B, "engagement_token": "whatever",
            })
            assert await resp.json() == {}
        assert [c[0] for c in calls] == ["default"]
        assert calls[0][1].get("casa_engagement_id") is None

    async def test_authenticated_id_mismatching_cwd_denies(self):
        """A valid credential for A with a payload cwd naming B is a spoof
        attempt (or corruption) — deny, no callback, no params selection."""
        calls: list = []
        registry = _Registry({ENG_A: _Rec(), ENG_B: _Rec(auth_token="tok-B")})
        app = _auth_app(default_cb=_recorder(calls, "default"),
                        exec_cb=_recorder(calls, "exec"), registry=registry)
        async with TestServer(app) as srv, TestClient(srv) as client:
            resp = await client.post("/hooks/resolve", json={
                "policy": "p",
                "payload": {"tool_name": "Bash",
                            "cwd": f"/data/engagements/{ENG_B}"},
                "engagement_id": ENG_A, "engagement_token": "tok-A",
            })
            out = (await resp.json())["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "cwd" in out["permissionDecisionReason"]
        assert calls == []

    async def test_unauthenticated_engagement_cwd_uses_defaults_no_identity(self):
        """The pre-#366 forgery: no credential, cwd claims an engagement.
        Executor params must NOT be selected from the cwd claim, and the
        callback must see no authenticated identity."""
        calls: list = []
        registry = _Registry({ENG_A: _Rec()})
        app = _auth_app(default_cb=_recorder(calls, "default"),
                        exec_cb=_recorder(calls, "exec"), registry=registry)
        async with TestServer(app) as srv, TestClient(srv) as client:
            resp = await client.post("/hooks/resolve", json={
                "policy": "p",
                "payload": {"tool_name": "Bash",
                            "cwd": f"/data/engagements/{ENG_A}"},
            })
            assert await resp.json() == {}
        assert [c[0] for c in calls] == ["default"]
        assert calls[0][1].get("casa_engagement_id") is None

    async def test_non_string_cwd_never_500s(self):
        """Terra r1: a non-string truthy cwd (e.g. a list) used to raise
        TypeError in the cwd regex OUTSIDE the deny wrapper — HTTP 500, which
        the shim's transport fail-open converts into an ALLOW. Malformed cwd
        is treated as absent (the field is advisory, never identity)."""
        calls: list = []
        registry = _Registry({ENG_A: _Rec()})
        app = _auth_app(default_cb=_recorder(calls, "default"),
                        exec_cb=_recorder(calls, "exec"), registry=registry)
        async with TestServer(app) as srv, TestClient(srv) as client:
            resp = await client.post("/hooks/resolve", json={
                "policy": "p",
                "payload": {"tool_name": "Bash", "cwd": ["x"]},
                "engagement_id": ENG_A, "engagement_token": "tok-A",
            })
            assert resp.status == 200
            assert await resp.json() == {}
        assert [c[0] for c in calls] == ["exec"]

    async def test_non_string_tool_name_denies_not_500(self):
        """Terra r1 (same class): a non-string tool_name raised in
        re.fullmatch — 500 — fail-open via the shim. Must be a structured
        deny."""
        calls: list = []
        registry = _Registry({ENG_A: _Rec()})
        app = _auth_app(default_cb=_recorder(calls, "default"),
                        exec_cb=_recorder(calls, "exec"), registry=registry)
        async with TestServer(app) as srv, TestClient(srv) as client:
            resp = await client.post("/hooks/resolve", json={
                "policy": "p",
                "payload": {"tool_name": 123},
            })
            assert resp.status == 200
            out = (await resp.json())["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert calls == []

    async def test_callback_receives_authenticated_identity_in_context(self):
        calls: list = []
        registry = _Registry({ENG_A: _Rec()})
        app = _auth_app(default_cb=_recorder(calls, "default"),
                        exec_cb=_recorder(calls, "exec"), registry=registry)
        async with TestServer(app) as srv, TestClient(srv) as client:
            await client.post("/hooks/resolve", json={
                "policy": "p",
                "payload": {"tool_name": "Bash",
                            "cwd": f"/data/engagements/{ENG_A}"},
                "engagement_id": ENG_A, "engagement_token": "tok-A",
            })
        assert calls[0][1].get("casa_engagement_id") == ENG_A


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
