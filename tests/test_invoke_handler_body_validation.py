"""L3: POST /invoke/{agent} must return 400 (not 500) for non-object JSON
bodies and must normalize an explicit "context": null instead of crashing.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.unit


class _StubResult:
    content = "ok"


class _StubBus:
    def __init__(self):
        self.last_msg = None

    async def request(self, msg, timeout=300):
        self.last_msg = msg
        return _StubResult()


def _make_app(bus):
    from casa_core import _make_invoke_handler
    from casa_core_middleware import cid_middleware
    from rate_limit import RateLimiter

    handler = _make_invoke_handler(
        webhook_rate_limiter=RateLimiter(capacity=0, window_s=60.0),  # 0 = disabled
        webhook_secret="",  # HMAC off
        bus=bus,
        assistant_role="assistant",
    )
    app = web.Application(middlewares=[cid_middleware])  # provides request["cid"], as prod does
    app.router.add_post("/invoke/{agent}", handler)
    return app


@pytest.mark.asyncio
async def test_non_dict_json_bodies_return_400_not_500():
    app = _make_app(_StubBus())
    async with TestClient(TestServer(app)) as client:
        for body in ("[1]", '"hi"', "42", "null"):
            r = await client.post(
                "/invoke/assistant", data=body,
                headers={"Content-Type": "application/json"},
            )
            assert r.status == 400, f"body={body} gave {r.status}"
            assert (await r.json()) == {"error": "invalid JSON body"}


@pytest.mark.asyncio
async def test_context_null_is_normalized_and_invocation_succeeds():
    bus = _StubBus()
    app = _make_app(bus)
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/invoke/assistant",
            json={"prompt": "status", "context": None},
        )
        assert r.status == 200
        assert (await r.json()) == {"response": "ok"}
        assert bus.last_msg.context.get("cid")  # cid was injected despite null context


@pytest.mark.asyncio
async def test_context_non_dict_string_is_normalized():
    """Bonus variant: a truthy non-dict context (e.g. a string) must also
    be normalized rather than raising at item-assignment."""
    bus = _StubBus()
    app = _make_app(bus)
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/invoke/assistant",
            json={"prompt": "status", "context": "abc"},
        )
        assert r.status == 200
        assert bus.last_msg.context.get("cid")


@pytest.mark.asyncio
async def test_missing_prompt_still_returns_400():
    """Existing contract must survive the refactor."""
    app = _make_app(_StubBus())
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/invoke/assistant", json={})
        assert r.status == 400
        assert (await r.json()) == {"error": "missing 'prompt' field"}
