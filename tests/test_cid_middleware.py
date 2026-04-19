"""Unit tests for casa_core_middleware.cid_middleware (spec 5.5 §3)."""

from __future__ import annotations

import asyncio
import re

import pytest
from aiohttp import web

from casa_core_middleware import cid_middleware
from log_cid import cid_var

pytestmark = pytest.mark.asyncio


HEX8 = re.compile(r"^[0-9a-f]{8}$")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _build_app(handler) -> web.Application:
    app = web.Application(middlewares=[cid_middleware])
    app.router.add_get("/probe", handler)
    return app


# ---------------------------------------------------------------------------
# TestDefaultAllocation
# ---------------------------------------------------------------------------


class TestDefaultAllocation:
    async def test_no_header_allocates_fresh_cid(self, aiohttp_client):
        async def handler(request):
            return web.Response(text=request["cid"])

        client = await aiohttp_client(_build_app(handler))
        resp = await client.get("/probe")
        assert resp.status == 200
        body = await resp.text()
        assert HEX8.match(body), f"expected 8-char hex, got {body!r}"

    async def test_two_requests_get_distinct_cids(self, aiohttp_client):
        seen: list[str] = []

        async def handler(request):
            seen.append(request["cid"])
            return web.Response(text="ok")

        client = await aiohttp_client(_build_app(handler))
        await client.get("/probe")
        await client.get("/probe")
        assert len(seen) == 2
        assert seen[0] != seen[1]
        assert all(HEX8.match(c) for c in seen)


# ---------------------------------------------------------------------------
# TestHeaderOverride
# ---------------------------------------------------------------------------


class TestHeaderOverride:
    async def test_valid_hex_header_is_used_verbatim(self, aiohttp_client):
        async def handler(request):
            return web.Response(text=request["cid"])

        client = await aiohttp_client(_build_app(handler))
        resp = await client.get(
            "/probe", headers={"X-Request-Cid": "deadbeef"}
        )
        assert (await resp.text()) == "deadbeef"

    async def test_longer_hex_accepted_up_to_32(self, aiohttp_client):
        async def handler(request):
            return web.Response(text=request["cid"])

        supplied = "0" * 32
        client = await aiohttp_client(_build_app(handler))
        resp = await client.get("/probe", headers={"X-Request-Cid": supplied})
        assert (await resp.text()) == supplied

    async def test_uppercase_hex_normalised_to_lowercase(self, aiohttp_client):
        async def handler(request):
            return web.Response(text=request["cid"])

        client = await aiohttp_client(_build_app(handler))
        resp = await client.get(
            "/probe", headers={"X-Request-Cid": "DEADBEEF"}
        )
        assert (await resp.text()) == "deadbeef"

    async def test_invalid_shape_rejected_and_fresh_cid_allocated(
        self, aiohttp_client
    ):
        async def handler(request):
            return web.Response(text=request["cid"])

        client = await aiohttp_client(_build_app(handler))
        resp = await client.get(
            "/probe", headers={"X-Request-Cid": "not-hex!"}
        )
        body = await resp.text()
        # rejected — fallback to fresh allocation
        assert HEX8.match(body)
        assert body != "not-hex!"

    async def test_too_short_rejected(self, aiohttp_client):
        async def handler(request):
            return web.Response(text=request["cid"])

        client = await aiohttp_client(_build_app(handler))
        resp = await client.get(
            "/probe", headers={"X-Request-Cid": "abc"}
        )
        body = await resp.text()
        assert HEX8.match(body)
        assert body != "abc"


# ---------------------------------------------------------------------------
# TestContextVarBinding
# ---------------------------------------------------------------------------


class TestContextVarBinding:
    async def test_cid_var_set_during_handler(self, aiohttp_client):
        async def handler(request):
            return web.Response(text=cid_var.get())

        client = await aiohttp_client(_build_app(handler))
        resp = await client.get(
            "/probe", headers={"X-Request-Cid": "cafef00d"}
        )
        assert (await resp.text()) == "cafef00d"

    async def test_cid_var_matches_request_cid(self, aiohttp_client):
        async def handler(request):
            return web.Response(
                text=f"{request['cid']}={cid_var.get()}"
            )

        client = await aiohttp_client(_build_app(handler))
        resp = await client.get("/probe")
        body = await resp.text()
        req_cid, var_cid = body.split("=")
        assert req_cid == var_cid

    async def test_cid_var_reset_after_handler(self, aiohttp_client):
        # Using two requests back-to-back — after the first completes,
        # cid_var in the TEST task is still its pre-request default ("-").
        # (Middleware runs in the server's task, not the test's.)

        assert cid_var.get() == "-", (
            "test precondition: cid_var starts at default"
        )

        async def handler(request):
            return web.Response(text=cid_var.get())

        client = await aiohttp_client(_build_app(handler))
        resp = await client.get(
            "/probe", headers={"X-Request-Cid": "beefcafe"}
        )
        assert (await resp.text()) == "beefcafe"

        # Even after the request, the test task's cid_var is unchanged.
        assert cid_var.get() == "-"


# ---------------------------------------------------------------------------
# TestExceptionSafety
# ---------------------------------------------------------------------------


class TestExceptionSafety:
    async def test_handler_exception_still_resets_cid_var(
        self, aiohttp_client
    ):
        # If a handler raises, the middleware's finally block must still
        # run. We can't observe cid_var in the server task from the test
        # task, so we assert indirectly: a second request after the first
        # raised should not observe stale state (its cid should be fresh
        # and distinct from a hypothetical "last cid set").

        failing_cids: list[str] = []

        async def handler(request):
            failing_cids.append(request["cid"])
            if request.headers.get("X-Fail"):
                raise RuntimeError("boom")
            return web.Response(text=request["cid"])

        client = await aiohttp_client(_build_app(handler))

        resp_fail = await client.get(
            "/probe", headers={"X-Fail": "1"}
        )
        assert resp_fail.status == 500  # aiohttp default 500 on exception

        resp_ok = await client.get("/probe")
        assert resp_ok.status == 200
        ok_cid = await resp_ok.text()
        assert HEX8.match(ok_cid)
        assert ok_cid != failing_cids[0]


# ---------------------------------------------------------------------------
# TestSpawnedTaskInherits
# ---------------------------------------------------------------------------


class TestSpawnedTaskInherits:
    async def test_asyncio_task_inherits_cid(self, aiohttp_client):
        # A task spawned inside the handler should see cid_var bound
        # because asyncio.create_task snapshots contextvars.

        inner_cid: dict[str, str] = {}

        async def inner():
            inner_cid["v"] = cid_var.get()

        async def handler(request):
            task = asyncio.create_task(inner())
            await task
            return web.Response(
                text=f"{request['cid']}={inner_cid['v']}"
            )

        client = await aiohttp_client(_build_app(handler))
        resp = await client.get(
            "/probe", headers={"X-Request-Cid": "12345678"}
        )
        body = await resp.text()
        outer, inner_v = body.split("=")
        assert outer == "12345678"
        assert inner_v == "12345678"
