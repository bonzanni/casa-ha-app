"""Tests for the /hooks/resolve loopback endpoint."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio


async def test_unknown_policy_blocks():
    from casa_core import _make_hooks_resolve_handler

    handler = _make_hooks_resolve_handler(hook_policies={})
    app = web.Application()
    app.router.add_post("/hooks/resolve", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "nonexistent", "payload": {}},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["decision"] == "block"
        assert "unknown policy" in body["reason"].lower()


async def test_known_policy_routes_to_registered_function():
    from casa_core import _make_hooks_resolve_handler

    def my_policy(payload: dict) -> dict:
        if payload.get("tool") == "Bash":
            return {"decision": "allow"}
        return {"decision": "block", "reason": "not bash"}

    handler = _make_hooks_resolve_handler(
        hook_policies={"my_policy": my_policy},
    )
    app = web.Application()
    app.router.add_post("/hooks/resolve", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        resp = await client.post(
            "/hooks/resolve",
            json={"policy": "my_policy", "payload": {"tool": "Bash"}},
        )
        body = await resp.json()
        assert body["decision"] == "allow"
