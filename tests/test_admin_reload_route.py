# tests/test_admin_reload_route.py
"""Tests for the POST /admin/reload route (Task E.1).

The route is registered on the internal Unix-socket aiohttp app by
``casa_core.start_internal_unix_runner``. casactl posts to it over
``/run/casa/internal.sock``; the handler is built by
``internal_handlers.build_admin_reload_handler``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio


def _make_app(*, runtime) -> web.Application:
    from internal_handlers import build_admin_reload_handler
    app = web.Application()
    app.router.add_post(
        "/admin/reload", build_admin_reload_handler(runtime=runtime),
    )
    return app


async def test_admin_reload_dispatches(monkeypatch) -> None:
    captured: dict = {}

    async def fake_dispatch(scope, *, runtime, role=None, include_env=False):
        captured.update(
            scope=scope, runtime=runtime, role=role, include_env=include_env,
        )
        return {
            "status": "ok", "scope": scope, "role": role,
            "ms": 1, "actions": ["fake"],
        }

    monkeypatch.setattr("reload.dispatch", fake_dispatch)
    runtime = MagicMock()

    app = _make_app(runtime=runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/reload",
            json={"scope": "agent", "role": "ellen", "include_env": False},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["scope"] == "agent"
        assert body["role"] == "ellen"

    assert captured["scope"] == "agent"
    assert captured["role"] == "ellen"
    assert captured["include_env"] is False
    assert captured["runtime"] is runtime


async def test_admin_reload_missing_scope_400() -> None:
    runtime = MagicMock()
    app = _make_app(runtime=runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/admin/reload", json={})
        assert resp.status == 400
        body = await resp.json()
        assert body["status"] == "error"
        assert body["kind"] == "scope_required"


async def test_admin_reload_bad_json_400() -> None:
    runtime = MagicMock()
    app = _make_app(runtime=runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/reload",
            data="this is not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["kind"] == "bad_json"


async def test_admin_reload_falls_back_to_active_runtime(monkeypatch) -> None:
    """If runtime kwarg is None at registration, handler reads agent.active_runtime."""
    captured: dict = {}

    async def fake_dispatch(scope, *, runtime, role=None, include_env=False):
        captured["runtime"] = runtime
        return {
            "status": "ok", "scope": scope, "role": role,
            "ms": 1, "actions": [],
        }

    monkeypatch.setattr("reload.dispatch", fake_dispatch)

    fallback_runtime = MagicMock(name="fallback_runtime")
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", fallback_runtime, raising=False)

    app = _make_app(runtime=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/reload", json={"scope": "policies"},
        )
        assert resp.status == 200

    assert captured["runtime"] is fallback_runtime


async def test_admin_reload_not_initialized_500(monkeypatch) -> None:
    """If both kwarg and active_runtime are None, return 500 not_initialized."""
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", None, raising=False)

    app = _make_app(runtime=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/reload", json={"scope": "policies"},
        )
        assert resp.status == 500
        body = await resp.json()
        assert body["kind"] == "not_initialized"


async def test_admin_reload_dispatch_error_returns_500(monkeypatch) -> None:
    async def fake_dispatch(scope, *, runtime, role=None, include_env=False):
        return {
            "status": "error", "scope": scope, "role": role,
            "kind": "boom", "message": "kaboom", "ms": 1, "actions": [],
        }

    monkeypatch.setattr("reload.dispatch", fake_dispatch)

    runtime = MagicMock()
    app = _make_app(runtime=runtime)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/reload", json={"scope": "agent"},
        )
        assert resp.status == 500
        body = await resp.json()
        assert body["status"] == "error"
        assert body["kind"] == "boom"
