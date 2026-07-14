"""End-to-end: PreToolUse -> /internal/hooks/resolve -> keyboard + broker -> verdict.

v0.37.2 (C-1) / v0.75.0 (W5/Sol B3,B4): exercises the full happy-path of the
permission-relay: the real `_make_internal_hooks_resolve_handler` factory
wired with the real `make_engagement_permission_relay` hook callback. The
test drives the resolver via HTTP and delivers the operator verdict via
``verdict_broker.BROKER.deliver`` (the same call
``channel_handlers._make_permission_verdict`` makes), asserting the
resolver's JSON response shape.

Both round-trips (allow + deny) prove:
- the hook resolves the engagement from cwd,
- it consults the engagement's frozen ``tools_allowed`` snapshot,
- it posts the Telegram inline keyboard exactly once,
- it registers + awaits the broker request,
- the resolver maps the hook return value to the right HTTP body shape.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import verdict_broker
from verdict_broker import VerdictBroker

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch):
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


class _Rec:
    def __init__(self, tools_allowed=(), topic_id=555, operator_id=1):
        self.status = "active"
        self.tools_allowed = tuple(tools_allowed)
        self.topic_id = topic_id
        self.origin = {"user_id": operator_id}


async def test_non_allow_listed_round_trip(_fresh_broker):
    """The hook posts a keyboard and awaits the broker; once a verdict is
    delivered, the /hooks/resolve response carries permissionDecision."""
    from internal_handlers import _make_internal_hooks_resolve_handler
    from hooks import make_engagement_permission_relay

    eid = "3" * 32
    registry = MagicMock()
    registry.get = MagicMock(return_value=_Rec(tools_allowed=()))
    telegram = MagicMock()
    telegram.update_topic_state = AsyncMock()
    telegram.post_perm_keyboard = AsyncMock(return_value=555)
    telegram.edit_perm_keyboard_outcome = AsyncMock()

    cb = make_engagement_permission_relay(
        engagement_registry=registry,
        telegram_channel=telegram,
        timeout_s=2.0,
    )
    handler = _make_internal_hooks_resolve_handler(
        hook_policies={"engagement_permission_relay": (r".*", cb)},
    )
    app = web.Application()
    app.router.add_post("/internal/hooks/resolve", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        # Deliver the verdict slightly after the request fires — the same
        # call channel_handlers._make_permission_verdict makes.
        async def feed_verdict():
            await asyncio.sleep(0.1)
            assert _fresh_broker.deliver(
                namespace="permission", scope=eid, request_id="tuid_e2e",
                option_index=0, actor_id=1,
            ) == "delivered"

        feeder = asyncio.create_task(feed_verdict())
        try:
            resp = await client.post(
                "/internal/hooks/resolve",
                json={
                    "policy": "engagement_permission_relay",
                    "payload": {
                        "tool_name": "Bash",
                        "tool_input": {"command": "curl ex"},
                        "cwd": f"/data/engagements/{eid}",
                        "tool_use_id": "tuid_e2e",
                    },
                },
            )
            body = await resp.json()
        finally:
            await feeder

    assert body == {}  # allow path -> pass-through (no hookSpecificOutput)
    telegram.post_perm_keyboard.assert_awaited_once()


async def test_deny_round_trip(_fresh_broker):
    """Same path with a deny verdict -- response is the deny hookSpecificOutput shape."""
    from internal_handlers import _make_internal_hooks_resolve_handler
    from hooks import make_engagement_permission_relay

    eid = "4" * 32
    registry = MagicMock()
    registry.get = MagicMock(return_value=_Rec(tools_allowed=()))
    telegram = MagicMock()
    telegram.update_topic_state = AsyncMock()
    telegram.post_perm_keyboard = AsyncMock(return_value=556)
    telegram.edit_perm_keyboard_outcome = AsyncMock()

    cb = make_engagement_permission_relay(
        engagement_registry=registry,
        telegram_channel=telegram,
        timeout_s=2.0,
    )
    handler = _make_internal_hooks_resolve_handler(
        hook_policies={"engagement_permission_relay": (r".*", cb)},
    )
    app = web.Application()
    app.router.add_post("/internal/hooks/resolve", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        async def feed_verdict():
            await asyncio.sleep(0.1)
            assert _fresh_broker.deliver(
                namespace="permission", scope=eid, request_id="tuid_deny",
                option_index=1, actor_id=1,
            ) == "delivered"

        feeder = asyncio.create_task(feed_verdict())
        try:
            resp = await client.post(
                "/internal/hooks/resolve",
                json={
                    "policy": "engagement_permission_relay",
                    "payload": {
                        "tool_name": "Bash",
                        "tool_input": {"command": "rm -rf /"},
                        "cwd": f"/data/engagements/{eid}",
                        "tool_use_id": "tuid_deny",
                    },
                },
            )
            body = await resp.json()
        finally:
            await feeder

    assert body == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Operator denied via Telegram",
        },
    }
