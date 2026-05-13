"""End-to-end: PreToolUse -> /internal/hooks/resolve -> keyboard + queue -> verdict.

v0.37.2 (C-1): exercises the full happy-path of the permission-relay fix:
the real `_make_internal_hooks_resolve_handler` factory wired with the
real `make_engagement_permission_relay` hook callback. The test drives
the resolver via HTTP, pushes verdicts onto the shared per-engagement
asyncio.Queue, and asserts the resolver's JSON response shape.

Both round-trips (allow + deny) prove:
- the hook resolves the engagement from cwd,
- it consults the engagement's frozen ``tools_allowed`` snapshot,
- it posts the Telegram inline keyboard exactly once,
- it awaits the per-engagement queue,
- the resolver maps the hook return value to the right HTTP body shape.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio


class _Rec:
    def __init__(self, tools_allowed=()):
        self.status = "active"
        self.tools_allowed = tuple(tools_allowed)


async def test_non_allow_listed_round_trip():
    """The hook posts a keyboard and awaits the queue; once a verdict
    is pushed, the /hooks/resolve response carries permissionDecision."""
    from internal_handlers import _make_internal_hooks_resolve_handler
    from hooks import make_engagement_permission_relay

    eid = "3" * 32
    registry = MagicMock()
    registry.get = MagicMock(return_value=_Rec(tools_allowed=()))
    telegram = MagicMock()
    telegram.update_topic_state = AsyncMock()
    telegram.post_perm_keyboard = AsyncMock()
    queues: dict[str, asyncio.Queue] = {eid: asyncio.Queue()}

    cb = make_engagement_permission_relay(
        engagement_registry=registry,
        telegram_channel=telegram,
        queues=queues,
        timeout_s=2.0,
    )
    handler = _make_internal_hooks_resolve_handler(
        hook_policies={"engagement_permission_relay": (r".*", cb)},
    )
    app = web.Application()
    app.router.add_post("/internal/hooks/resolve", handler)

    async with TestServer(app) as srv, TestClient(srv) as client:
        # Push the verdict slightly after the request fires.
        async def feed_verdict():
            await asyncio.sleep(0.1)
            await queues[eid].put(
                {"request_id": "tuid_e2e", "verdict": "allow"},
            )

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


async def test_deny_round_trip():
    """Same path with a deny verdict -- response is the deny hookSpecificOutput shape."""
    from internal_handlers import _make_internal_hooks_resolve_handler
    from hooks import make_engagement_permission_relay

    eid = "4" * 32
    registry = MagicMock()
    registry.get = MagicMock(return_value=_Rec(tools_allowed=()))
    telegram = MagicMock()
    telegram.update_topic_state = AsyncMock()
    telegram.post_perm_keyboard = AsyncMock()
    queues: dict[str, asyncio.Queue] = {eid: asyncio.Queue()}

    cb = make_engagement_permission_relay(
        engagement_registry=registry,
        telegram_channel=telegram,
        queues=queues,
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
            await queues[eid].put(
                {"request_id": "tuid_deny", "verdict": "deny"},
            )

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
            "permissionDecisionReason": "operator denied via Telegram",
        },
    }
