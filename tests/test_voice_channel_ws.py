"""Spec §3.2, §4.3 — WebSocket transport + stt_start prewarm dedup."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from bus import BusMessage, MessageBus, MessageType
from channels.voice.channel import VoiceChannel


class _StreamingAgent:
    def __init__(self, bus, role): self._role = role
    async def handle_message(self, msg: BusMessage):
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("[warm] Hi.")
            await on_token("[warm] Hi. There.")
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="[warm] Hi. There.", reply_to=msg.id,
            channel=msg.channel, context=msg.context,
        )


class _FakeCfg:
    class tts: tag_dialect = "square_brackets"
    memory = type("M", (), {"token_budget": 800})()
    role = "butler"
    voice_errors: dict = {}


@pytest.fixture
async def ws_app():
    bus = MessageBus()
    agent = _StreamingAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop = asyncio.create_task(bus.run_agent_loop("butler"))

    memory = AsyncMock()
    memory.ensure_session = AsyncMock(return_value=None)
    memory.get_context = AsyncMock(return_value="")
    memory.add_turn = AsyncMock(return_value=None)

    ch = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret="",
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeCfg()},
        memory=memory, idle_timeout=300,
    )
    app = web.Application()
    ch.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        yield client, bus, memory, ch
    loop.cancel()


@pytest.mark.asyncio
class TestWSTurn:
    async def test_stt_start_then_utterance(self, ws_app):
        client, _, memory, _ = ws_app
        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({
                "type": "stt_start", "session_key": "voice:s",
                "scope_id": "s", "context": {"device_id": "kitchen"},
            })
            await ws.send_json({
                "type": "utterance", "utterance_id": "u1",
                "text": "hi", "agent_role": "butler", "scope_id": "s",
            })
            got = []
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                frame = json.loads(msg.data)
                got.append(frame["type"])
                if frame["type"] == "done":
                    break
            assert "block" in got
            assert got[-1] == "done"
            # Prewarm fired at least once.
            assert memory.ensure_session.await_count >= 1

    async def test_stt_start_dedup(self, ws_app):
        client, _, memory, channel = ws_app

        # Make prewarm block so the first task is still live when the second
        # stt_start arrives (otherwise the first may finish between frames).
        ensure_block = asyncio.Event()
        async def slow_ensure(*args, **kwargs):
            await ensure_block.wait()
        memory.ensure_session = slow_ensure

        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({"type": "stt_start", "scope_id": "s"})
            await ws.send_json({"type": "stt_start", "scope_id": "s"})
            await asyncio.sleep(0.05)
            sess = channel.pool.get("s")
            assert sess is not None
            assert sess.prewarm_task is not None
            # Release so the ws handler can close cleanly.
            ensure_block.set()

    async def test_cancel_stops_in_flight(self, ws_app):
        client, bus, _, channel = ws_app

        # Replace the handler with one that blocks until cancelled.
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def slow(msg: BusMessage):
            on_token = msg.context.get("_on_token")
            if on_token:
                await on_token("starting")
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        bus.handlers["butler"] = slow

        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({
                "type": "utterance", "utterance_id": "u1",
                "text": "x", "agent_role": "butler", "scope_id": "s",
            })
            await asyncio.wait_for(started.wait(), timeout=2.0)
            await ws.send_json({"type": "cancel", "utterance_id": "u1"})
            await asyncio.wait_for(cancelled.wait(), timeout=3.0)


# ---------------------------------------------------------------------------
# Rate limiting — per-scope_id (spec 5.2 §8)
# ---------------------------------------------------------------------------


@pytest.fixture
async def voice_ws_app_with_limiter(request):
    from rate_limit import RateLimiter

    capacity = getattr(request, "param", 2)

    bus = MessageBus()
    agent = _StreamingAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

    memory = AsyncMock()
    memory.ensure_session = AsyncMock(return_value=None)
    memory.get_context = AsyncMock(return_value="")
    memory.add_turn = AsyncMock(return_value=None)

    limiter = RateLimiter(capacity=capacity, window_s=60.0)

    channel = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret="",
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeCfg()},
        memory=memory, idle_timeout=300,
        rate_limiter=limiter,
    )

    app = web.Application()
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client
    loop_task.cancel()


@pytest.mark.asyncio
class TestRateLimit:
    @pytest.mark.parametrize("voice_ws_app_with_limiter", [1], indirect=True)
    async def test_ws_rate_limit_emits_error_frame(
        self, voice_ws_app_with_limiter,
    ):
        client = voice_ws_app_with_limiter
        async with client.ws_connect("/api/converse/ws") as ws:
            # First utterance admitted.
            await ws.send_json({
                "type": "utterance", "utterance_id": "u1",
                "scope_id": "user-w", "agent_role": "butler",
                "text": "hi",
            })

            got_done_u1 = False
            got_rate_limit_u2 = False
            sent_second = False

            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("type") == "done" and data.get("utterance_id") == "u1":
                    got_done_u1 = True
                    # Fire the second utterance — bucket is exhausted.
                    await ws.send_json({
                        "type": "utterance", "utterance_id": "u2",
                        "scope_id": "user-w", "agent_role": "butler",
                        "text": "hello again",
                    })
                    sent_second = True
                elif (
                    data.get("type") == "error"
                    and data.get("utterance_id") == "u2"
                    and data.get("kind") == "rate_limit"
                ):
                    got_rate_limit_u2 = True
                    break

            assert got_done_u1, "first utterance must complete"
            assert sent_second
            assert got_rate_limit_u2, "second utterance must get kind=rate_limit"

    @pytest.mark.parametrize("voice_ws_app_with_limiter", [0], indirect=True)
    async def test_ws_capacity_zero_is_unlimited(
        self, voice_ws_app_with_limiter,
    ):
        client = voice_ws_app_with_limiter
        async with client.ws_connect("/api/converse/ws") as ws:
            for i in range(5):
                uid = f"u{i}"
                await ws.send_json({
                    "type": "utterance", "utterance_id": uid,
                    "scope_id": "u", "agent_role": "butler",
                    "text": f"msg-{i}",
                })
            done_ids: set[str] = set()
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("type") == "done":
                    done_ids.add(data["utterance_id"])
                if len(done_ids) == 5:
                    break
            assert done_ids == {f"u{i}" for i in range(5)}
