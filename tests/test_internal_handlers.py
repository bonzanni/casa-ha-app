# tests/test_internal_handlers.py
"""Unit tests for internal_handlers.py (Plan 4b Phase 3.6)."""
from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio


# ----- Test fixtures: minimal engagement registry + tool dispatch ----------


class _FakeRecord:
    def __init__(self, eng_id: str, status: str = "active") -> None:
        self.id = eng_id
        self.status = status


class _FakeRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, _FakeRecord] = {}

    def add(self, rec: _FakeRecord) -> None:
        self._by_id[rec.id] = rec

    def get(self, eng_id: str) -> _FakeRecord | None:
        return self._by_id.get(eng_id)


async def _ok_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Returns args back as a 'content' block, MCP-style."""
    return {"content": [{"type": "text", "text": json.dumps(args)}]}


async def _engagement_aware_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Reads engagement_var to verify ContextVar binding works."""
    from tools import engagement_var
    rec = engagement_var.get(None)
    rec_id = rec.id if rec is not None else None
    return {"content": [{"type": "text", "text": json.dumps({"eng": rec_id})}]}


async def _raising_tool(_args: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("boom")


def _make_app(*, dispatch: dict, registry: _FakeRegistry,
              hook_policies: dict | None = None) -> web.Application:
    """Build a tiny aiohttp app exposing the two internal handlers."""
    from internal_handlers import (
        _make_internal_tools_call_handler,
        _make_internal_hooks_resolve_handler,
    )
    app = web.Application()
    app.router.add_post(
        "/internal/tools/call",
        _make_internal_tools_call_handler(
            tool_dispatch=dispatch, engagement_registry=registry,
        ),
    )
    app.router.add_post(
        "/internal/hooks/resolve",
        _make_internal_hooks_resolve_handler(
            hook_policies=hook_policies or {},
        ),
    )
    return app


async def test_tools_call_known_tool_returns_result() -> None:
    reg = _FakeRegistry()
    app = _make_app(dispatch={"ok_tool": _ok_tool}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "ok_tool", "arguments": {"x": 1}, "engagement_id": None},
        )
        assert resp.status == 200
        body = await resp.json()
        # Internal handler returns the bare tool result (no JSON-RPC wrapping).
        assert body == {"content": [{"type": "text", "text": '{"x": 1}'}]}


async def test_tools_call_unknown_tool_returns_error_object() -> None:
    reg = _FakeRegistry()
    app = _make_app(dispatch={}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "nonesuch", "arguments": {}, "engagement_id": None},
        )
        assert resp.status == 200  # 200 — protocol-level not transport-level
        body = await resp.json()
        assert body == {"error": {"code": -32602, "message": "Unknown tool: nonesuch"}}


async def test_tools_call_missing_name_returns_error_object() -> None:
    reg = _FakeRegistry()
    app = _make_app(dispatch={"ok_tool": _ok_tool}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"arguments": {}, "engagement_id": None},
        )
        body = await resp.json()
        assert body == {"error": {"code": -32602, "message": "missing name"}}


async def test_tools_call_engagement_id_binds_contextvar() -> None:
    reg = _FakeRegistry()
    rec = _FakeRecord(eng_id="abc-123", status="active")
    reg.add(rec)
    app = _make_app(dispatch={"eng": _engagement_aware_tool}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "eng", "arguments": {}, "engagement_id": "abc-123"},
        )
        body = await resp.json()
        text = json.loads(body["content"][0]["text"])
        assert text == {"eng": "abc-123"}


async def test_tools_call_inactive_engagement_binds_none() -> None:
    """Defense-in-depth: only UNDERGOING (status=='active') engagements get
    bound — finalized/cancelled records become None."""
    reg = _FakeRegistry()
    rec = _FakeRecord(eng_id="fin-1", status="completed")
    reg.add(rec)
    app = _make_app(dispatch={"eng": _engagement_aware_tool}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "eng", "arguments": {}, "engagement_id": "fin-1"},
        )
        body = await resp.json()
        text = json.loads(body["content"][0]["text"])
        assert text == {"eng": None}


async def test_tools_call_handler_exception_returns_error_object() -> None:
    reg = _FakeRegistry()
    app = _make_app(dispatch={"raises": _raising_tool}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            json={"name": "raises", "arguments": {}, "engagement_id": None},
        )
        body = await resp.json()
        assert body["error"]["code"] == -32001  # tool-level error, distinct from -32000
        assert "boom" in body["error"]["message"]


async def test_tools_call_malformed_json_returns_error_object() -> None:
    reg = _FakeRegistry()
    app = _make_app(dispatch={}, registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/tools/call",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        body = await resp.json()
        assert body == {"error": {"code": -32700, "message": "Parse error"}}


# Append to tests/test_internal_handlers.py


async def _allow_callback(_payload, _ctx, _opts):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
    }}


async def _deny_callback(_payload, _ctx, _opts):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "blocked",
    }}


async def _none_callback(_payload, _ctx, _opts):
    return None


async def _raising_callback(_payload, _ctx, _opts):
    raise RuntimeError("hook boom")


async def test_hooks_resolve_unknown_policy_denies() -> None:
    reg = _FakeRegistry()
    app = _make_app(dispatch={}, registry=reg, hook_policies={})
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            json={"policy": "no_such", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "unknown policy" in body["hookSpecificOutput"]["permissionDecisionReason"]


async def test_hooks_resolve_known_policy_invokes_callback() -> None:
    reg = _FakeRegistry()
    policies = {"deny_all": ("Bash", _deny_callback)}
    app = _make_app(dispatch={}, registry=reg, hook_policies=policies)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            json={"policy": "deny_all", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_hooks_resolve_matcher_mismatch_returns_empty_allow() -> None:
    """Defense-in-depth: if the payload's tool_name doesn't fullmatch the
    policy's matcher regex, return empty body (= allow). Mirrors v0.13.1
    behavior."""
    reg = _FakeRegistry()
    policies = {"bash_only": ("Bash", _deny_callback)}
    app = _make_app(dispatch={}, registry=reg, hook_policies=policies)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            json={"policy": "bash_only", "payload": {"tool_name": "Read"}},
        )
        body = await resp.json()
        assert body == {}


async def test_hooks_resolve_callback_none_means_allow() -> None:
    """A HookCallback returning None (no decision) -> empty body = allow."""
    reg = _FakeRegistry()
    policies = {"silent": ("Bash", _none_callback)}
    app = _make_app(dispatch={}, registry=reg, hook_policies=policies)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            json={"policy": "silent", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body == {}


async def test_hooks_resolve_callback_exception_denies() -> None:
    """Fail-closed: a raising callback denies. Matches v0.13.1 behavior."""
    reg = _FakeRegistry()
    policies = {"buggy": ("Bash", _raising_callback)}
    app = _make_app(dispatch={}, registry=reg, hook_policies=policies)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            json={"policy": "buggy", "payload": {"tool_name": "Bash"}},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "raised" in body["hookSpecificOutput"]["permissionDecisionReason"]


async def test_hooks_resolve_malformed_json_denies() -> None:
    """Fail-closed on malformed body too -- matches v0.13.1."""
    reg = _FakeRegistry()
    app = _make_app(dispatch={}, registry=reg, hook_policies={})
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/hooks/resolve",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        body = await resp.json()
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
