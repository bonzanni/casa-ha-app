"""N-2 (v0.36.0). The wildcard ``/webhook/{name}`` handler must consult
the trigger registry's per-boot allowlist and 404 unknown names. Known
names dispatch to the role registered with the trigger, not the
hardcoded assistant_role.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


pytestmark = pytest.mark.asyncio


def _make_bus():
    bus = MagicMock()
    bus.send = AsyncMock()
    return bus


def _make_registry(targets: dict[str, str]):
    """Stand-in TriggerRegistry exposing only get_webhook_target."""
    reg = MagicMock()
    reg.get_webhook_target = lambda name: targets.get(name)
    return reg


def _hmac(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _build_app(
    *,
    secret: str = "",
    targets: dict[str, str] | None = None,
    default_role: str = "assistant",
    bus=None,
):
    from casa_core import _make_webhook_handler
    from rate_limit import RateLimiter

    targets = targets or {}
    bus = bus or _make_bus()
    limiter = RateLimiter(capacity=0, window_s=60.0)  # 0 = disabled

    handler = _make_webhook_handler(
        webhook_rate_limiter=limiter,
        webhook_secret=secret,
        trigger_registry=_make_registry(targets),
        default_role=default_role,
        bus=bus,
    )

    app = web.Application()
    app.router.add_post("/webhook/{name}", handler)
    # The handler's cid lookup uses ``request.get("cid") or new_cid()``,
    # so missing cid is fine — production wires log_cid middleware.
    return app, bus


class TestWebhookAllowlist:
    async def test_unknown_name_returns_404(self):
        """N-2: POST /webhook/<unknown> must 404, not dispatch."""
        app, bus = await _build_app(targets={"known": "assistant"})
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/webhook/never-registered", json={})
            assert r.status == 404
            payload = await r.json()
            assert payload == {"error": "unknown webhook"}
            bus.send.assert_not_called()

    async def test_known_name_dispatches_and_returns_200(self):
        app, bus = await _build_app(targets={"probe": "assistant"})
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/webhook/probe", json={"x": 1})
            assert r.status == 200
            payload = await r.json()
            assert payload == {"status": "accepted"}
            bus.send.assert_awaited_once()
            msg = bus.send.call_args.args[0]
            assert msg.target == "assistant"
            assert msg.context["webhook_name"] == "probe"

    async def test_known_name_dispatches_to_registered_role(self):
        """A webhook trigger registered for role=butler dispatches there,
        not to the hardcoded default."""
        app, bus = await _build_app(
            targets={"b1": "butler"}, default_role="assistant",
        )
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/webhook/b1", json={})
            assert r.status == 200
            msg = bus.send.call_args.args[0]
            assert msg.target == "butler"

    async def test_invalid_hmac_returns_401_before_404(self):
        """HMAC validation must run before name validation so a bad-sig
        request to an unknown name still 401s (not 404, which would leak
        the existence/non-existence of the name)."""
        app, bus = await _build_app(
            secret="topsecret", targets={"known": "assistant"},
        )
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/webhook/never-registered",
                data=b"{}",
                headers={"X-Webhook-Signature": "wrong"},
            )
            assert r.status == 401
            bus.send.assert_not_called()

    async def test_payload_context_never_enters_bus_message_context(self):
        """r2-B6 (A:§1): the wildcard handler builds a Casa-OWNED context
        (webhook_name/cid only) and embeds the payload in message CONTENT —
        it must NOT start propagating a caller-supplied payload["context"]
        dict into BusMessage.context (that would let an external webhook
        caller spoof provenance-bearing keys like execution_role)."""
        app, bus = await _build_app(targets={"probe": "assistant"})
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/webhook/probe", json={
                "x": 1,
                "context": {
                    "execution_role": "butler", "message_type": "channel_in",
                    "source": "telegram", "synthetic": "button",
                    "smuggled": "should-not-appear",
                },
            })
            assert r.status == 200
            msg = bus.send.call_args.args[0]
            # Precise contract: only Casa-owned keys are present.
            assert set(msg.context.keys()) <= {"webhook_name", "cid"}
            assert "execution_role" not in msg.context
            assert "smuggled" not in msg.context
            assert "synthetic" not in msg.context

    async def test_valid_hmac_unknown_name_returns_404(self):
        """Valid HMAC but unknown name still 404s (defense-in-depth):
        operator removed a webhook trigger, secret unchanged, replays must
        fail with the right status."""
        secret = "topsecret"
        body = b"{}"
        sig = _hmac(secret, body)
        app, bus = await _build_app(
            secret=secret, targets={"known": "assistant"},
        )
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/webhook/never-registered",
                data=body,
                headers={"X-Webhook-Signature": sig},
            )
            assert r.status == 404
            bus.send.assert_not_called()
