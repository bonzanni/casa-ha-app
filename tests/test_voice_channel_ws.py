"""Spec §3.2, §4.3 — WebSocket transport + stt_start prewarm dedup."""

import asyncio
import gc
import json
import logging
import weakref
from unittest.mock import AsyncMock

import pytest
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from bus import BusMessage, MessageBus, MessageType
from channels.voice.channel import VoiceChannel

pytestmark = pytest.mark.unit


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
    memory.profile = AsyncMock(return_value="")

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
                "type": "stt_start", "session_key": "voice-s",
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
            # profile() must NOT be called on voice turns — overlay is not
            # pushed at 'friends' clearance; the overlay prewarm was removed.
            assert memory.profile.await_count == 0

    async def test_stt_start_ensures_session(self, ws_app):
        """stt_start ensures a VoiceSession is created in the pool; sending
        it twice is idempotent (same session, no duplicate tasks).  The
        obsolete overlay prewarm has been removed — no profile() is called."""
        client, _, memory, channel = ws_app

        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({"type": "stt_start", "scope_id": "s"})
            await ws.send_json({"type": "stt_start", "scope_id": "s"})
            await asyncio.sleep(0.05)
            sess = channel.pool.get("s")
            assert sess is not None
            # No prewarm task — schedule_prewarm is no longer called.
            assert sess.prewarm_task is None
            # No profile() call — overlay not used for voice.
            assert memory.profile.await_count == 0

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

    async def test_client_context_cannot_clobber_computed_keys(self, ws_app):
        """L59/L8 (WS side): a client-supplied context dict must not
        override the channel-computed chat_id/cid/utterance_id."""
        client, bus, _, _ = ws_app
        captured = {}
        orig_request = bus.request

        async def spy_request(msg, timeout=300):
            captured["msg"] = msg
            return await orig_request(msg, timeout=timeout)

        bus.request = spy_request

        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({
                "type": "utterance", "utterance_id": "forged-uid",
                "text": "hi", "agent_role": "butler", "scope_id": "s",
                "context": {
                    "chat_id": "living-room",
                    "cid": "client-forged-cid",
                    "device_id": "kitchen",
                },
            })
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                frame = json.loads(msg.data)
                if frame["type"] == "done":
                    break

        ctx = captured["msg"].context
        assert ctx["chat_id"] == "s"
        assert ctx["cid"] != "client-forged-cid"
        assert ctx["device_id"] == "kitchen"

    async def test_ws_utterance_task_pruned_and_exception_retrieved(
        self, ws_app, caplog, monkeypatch,
    ):
        """L60/L9: finished utterance tasks must be pruned from the
        per-connection tasks dict, and a task that finishes with an
        exception must have that exception retrieved (never logged as
        'never retrieved') and surfaced as a warning."""
        client, _, _, channel = ws_app
        task_refs: list[weakref.ref] = []

        async def stub_ok(ws, frame, uid):
            task_refs.append(weakref.ref(asyncio.current_task()))
            await ws.send_json({"type": "done", "utterance_id": uid})

        monkeypatch.setattr(channel, "_run_ws_utterance", stub_ok)

        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({
                "type": "utterance", "utterance_id": "u1", "text": "hi",
                "agent_role": "butler", "scope_id": "s",
            })
            msg = await ws.receive_json(timeout=2.0)
            assert msg["type"] == "done"
            await asyncio.sleep(0.05)  # let the done-callback run
            gc.collect()
            # Pruned from the per-connection dict => nothing references the
            # finished task anymore, so the weakref must be dead WHILE the
            # WS is still open.
            assert task_refs and task_refs[0]() is None

            # Exception arm: a failing utterance task must be reaped +
            # logged, never left as an unretrieved-exception task.
            async def stub_fail(ws_, frame, uid):
                raise ConnectionResetError("Cannot write to closing transport")
            monkeypatch.setattr(channel, "_run_ws_utterance", stub_fail)
            with caplog.at_level("WARNING"):
                await ws.send_json({
                    "type": "utterance", "utterance_id": "u2", "text": "x",
                    "agent_role": "butler", "scope_id": "s",
                })
                await asyncio.sleep(0.1)
            gc.collect()
            assert any("utterance task failed" in r.message for r in caplog.records)
            assert not any("never retrieved" in r.message for r in caplog.records)


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
    memory.profile = AsyncMock(return_value="")

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


# ---------------------------------------------------------------------------
# AR-B prefix-divergence guard + AR-C time-cap (2026-07-11 voice partial-
# streaming design §2 point 3, §6) — WS side.
# ---------------------------------------------------------------------------


class _NonPrefixAgent:
    """Simulates a divergent/retried turn: the two on_token calls do NOT
    form a growing prefix sequence (AR-B)."""

    def __init__(self, bus, role): self._role = role

    async def handle_message(self, msg: BusMessage):
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("Attempt one talking ")       # no sentence mark yet
            await on_token("Attempt two is unrelated.")   # does NOT extend the above
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="Attempt two is unrelated.", reply_to=msg.id,
            channel=msg.channel, context=msg.context,
        )


class _ShrinkingAgent:
    """The second cumulative is SHORTER than the first (a canonical
    correction that retracts already-flushed text)."""

    def __init__(self, bus, role): self._role = role

    async def handle_message(self, msg: BusMessage):
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("Hello there my friend.")   # flushes immediately
            await on_token("Hi.")                        # SDK correction: shorter
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="Hi.", reply_to=msg.id,
            channel=msg.channel, context=msg.context,
        )


class _StallingAgent:
    """AR-C: emits two deltas with a >1.5s monkeypatched clock gap between
    them, mid-sentence (no natural cut)."""

    def __init__(self, bus, role, clock):
        self._role = role
        self._clock = clock

    async def handle_message(self, msg: BusMessage):
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("word, ")
            self._clock[0] += 2.0  # advance past the 1.5s cap
            await on_token("word, more text")
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="word, more text", reply_to=msg.id,
            channel=msg.channel, context=msg.context,
        )


@pytest.fixture
async def ws_app_nonprefix():
    bus = MessageBus()
    agent = _NonPrefixAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop = asyncio.create_task(bus.run_agent_loop("butler"))

    ch = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret="",
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeCfg()},
        memory=AsyncMock(), idle_timeout=300,
    )
    app = web.Application()
    ch.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        yield client
    loop.cancel()


@pytest.fixture
async def ws_app_shrinking():
    bus = MessageBus()
    agent = _ShrinkingAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop = asyncio.create_task(bus.run_agent_loop("butler"))

    ch = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret="",
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeCfg()},
        memory=AsyncMock(), idle_timeout=300,
    )
    app = web.Application()
    ch.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        yield client
    loop.cancel()


@pytest.fixture
async def ws_app_stalling(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        "channels.voice.prosodic.time.monotonic", lambda: clock[0],
    )
    bus = MessageBus()
    agent = _StallingAgent(bus, "butler", clock)
    bus.register("butler", agent.handle_message)
    loop = asyncio.create_task(bus.run_agent_loop("butler"))

    ch = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret="",
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeCfg()},
        memory=AsyncMock(), idle_timeout=300,
    )
    app = web.Application()
    ch.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        yield client
    loop.cancel()


async def _collect_ws_frames(client, *, scope_id: str, uid: str) -> list[dict]:
    frames: list[dict] = []
    async with client.ws_connect("/api/converse/ws") as ws:
        await ws.send_json({
            "type": "utterance", "utterance_id": uid,
            "text": "hi", "agent_role": "butler", "scope_id": scope_id,
        })
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            frames.append(frame)
            if frame["type"] == "done":
                break
    return frames


@pytest.mark.asyncio
class TestARBGuardWS:
    async def test_nonprefix_cumulative_resets_splitter_and_logs_debug(
        self, ws_app_nonprefix, caplog,
    ):
        with caplog.at_level(logging.DEBUG, logger="channels.voice.channel"):
            frames = await _collect_ws_frames(
                ws_app_nonprefix, scope_id="s-ar-b-ws", uid="u-ar-b",
            )

        assert any(f["type"] == "done" for f in frames)
        block_texts = [f["text"] for f in frames if f["type"] == "block"]
        # The pre-reset buffered text ("Attempt one talking ", no sentence
        # mark, never flushed) is discarded on reset; the fresh splitter
        # renders attempt two's text cleanly — no garbled concatenation.
        assert block_texts == ["Attempt two is unrelated."], block_texts
        assert any(
            "non-prefix cumulative" in r.getMessage()
            for r in caplog.records
            if r.name == "channels.voice.channel"
        ), [r.getMessage() for r in caplog.records]

    async def test_shrinking_cumulative_does_not_throw(self, ws_app_shrinking):
        frames = await _collect_ws_frames(
            ws_app_shrinking, scope_id="s-ar-b-shrink-ws", uid="u-shrink",
        )

        types = [f["type"] for f in frames]
        assert "done" in types
        assert "error" not in types
        block_texts = [f["text"] for f in frames if f["type"] == "block"]
        # The first (already-flushed) block survives; the shrink is
        # rendered as a fresh, clean block of its own — no crash, no
        # garbage/empty-string block.
        assert block_texts == ["Hello there my friend.", "Hi."], block_texts


@pytest.mark.asyncio
class TestARCTimeCapWS:
    async def test_stall_mid_sentence_forces_clause_preferring_block(
        self, ws_app_stalling,
    ):
        frames = await _collect_ws_frames(
            ws_app_stalling, scope_id="s-ar-c-ws", uid="u-arc",
        )

        block_texts = [f["text"] for f in frames if f["type"] == "block"]
        # The >1.5s stall forces a cap block on the rightmost clause mark
        # (the comma) rather than waiting for a sentence mark or hard-
        # cutting mid-word; the remainder is flushed at turn end.
        assert block_texts, "expected the time-cap to force a block mid-turn"
        assert block_texts[0].rstrip() == "word,", block_texts
        assert "more text" in "".join(block_texts)
