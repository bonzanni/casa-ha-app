"""Tests for casa_reload — scope-dispatching in-process reload (v0.35.0+)."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def configurator_origin():
    """Set origin_var so the role guard lets the call through."""
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


class TestCasaReloadTool:
    async def test_no_arg_returns_scope_required(self, configurator_origin):
        from tools import casa_reload
        result = await casa_reload.handler({})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "scope_required"


class TestCasaReloadScope:
    async def test_unknown_scope_returns_error(
        self, configurator_origin, monkeypatch,
    ):
        # Stub agent.active_runtime + reload.dispatch.
        import agent as agent_mod
        runtime_mock = MagicMock()
        agent_mod.active_runtime = runtime_mock

        from tools import casa_reload
        result = await casa_reload.handler({"scope": "nope"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        # The dispatcher itself returns kind='unknown_scope' for unknown handlers;
        # here we accept either an early local guard or the dispatcher's response.
        assert payload["kind"] in ("unknown_scope", "scope_required") or "scope" in payload["message"].lower()

    async def test_no_scope_arg_returns_error(self, configurator_origin):
        from tools import casa_reload
        result = await casa_reload.handler({})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "scope_required"

    async def test_dispatch_calls_reload_module(
        self, configurator_origin, monkeypatch,
    ):
        import agent as agent_mod
        from tools import casa_reload

        runtime_mock = MagicMock()
        agent_mod.active_runtime = runtime_mock

        captured = {}

        async def fake_dispatch(scope, *, runtime, role=None, include_env=False):
            captured.update(scope=scope, role=role, include_env=include_env)
            return {"status": "ok", "scope": scope, "role": role,
                    "ms": 1, "actions": ["fake"]}
        monkeypatch.setattr("reload.dispatch", fake_dispatch)

        result = await casa_reload.handler({"scope": "agent", "role": "ellen"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "ok"
        assert payload["scope"] == "agent"
        assert payload["role"] == "ellen"
        assert captured == {"scope": "agent", "role": "ellen", "include_env": False}

    async def test_runtime_not_initialized(self, configurator_origin, monkeypatch):
        # If active_runtime is None, the tool returns not_initialized.
        import agent as agent_mod
        agent_mod.active_runtime = None
        from tools import casa_reload
        result = await casa_reload.handler({"scope": "full"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "not_initialized"


class TestCasaReloadRoleGuard:
    """Role guard still applies — a non-configurator caller is refused
    even if they pass a valid scope."""

    async def test_no_origin_no_engagement_refused(self):
        from tools import casa_reload
        # No origin_var, no engagement_var bound — refuse.
        result = await casa_reload.handler({"scope": "full"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "not_authorized"

    async def test_assistant_role_refused(self):
        import agent as agent_mod
        from tools import casa_reload
        tok = agent_mod.origin_var.set({"role": "assistant"})
        try:
            result = await casa_reload.handler({"scope": "full"})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"
        assert "'assistant'" in payload["message"]

    async def test_specialist_role_refused(self):
        import agent as agent_mod
        from tools import casa_reload
        tok = agent_mod.origin_var.set({"role": "finance"})
        try:
            result = await casa_reload.handler({"scope": "full"})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])
        assert payload["kind"] == "not_authorized"
