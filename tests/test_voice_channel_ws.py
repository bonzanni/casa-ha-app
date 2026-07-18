"""Spec §3.2, §4.3 — WebSocket transport + stt_start prewarm dedup."""

import asyncio
import gc
import hashlib
import hmac
import json
import logging
import weakref
from unittest.mock import AsyncMock

import pytest
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from bus import BusMessage, MessageBus, MessageType
from channels.voice.channel import VoiceChannel, VoiceHandoffReservation
from channels.voice.routes import VoiceWsConnection
from error_kinds import VoiceToolLoopError

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
    voice_errors: dict = {
        "voice_tool_loop": (
            "[apologetic] I couldn't resolve that cleanly. "
            "Try naming the device again?"
        ),
    }
    channels: list[str] = ["ha_voice"]


class _TextOnlyCfg(_FakeCfg):
    channels: list[str] = ["webhook"]


class _DeliverySpy:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict]] = []
        self.called = asyncio.Event()

    async def handle(self, connection, frame):
        self.calls.append((connection, frame))
        self.called.set()

    async def route_connected(self, _route):
        return None


class _HandoffJob:
    """Minimal durable job shape delivered by the Task-3 commit seam."""

    id = "job-1"
    handoff_id = "handoff-1"
    specialist_display_name = "Finance"


class _RecordingWs:
    """Connection double that proves the write precedes request cancellation."""

    voice_route_id = "route-1"
    voice_route_capabilities = frozenset({
        "background_jobs", "satellite_announce", "voice_handoff",
    })
    voice_job_control_id = "route-1"

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.write_completed = asyncio.Event()

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)
        await asyncio.sleep(0)
        self.write_completed.set()


class _HandoffingBus:
    """Models a Concierge handler committing after Task-3 durability."""

    def __init__(self) -> None:
        self.request_cancelled = asyncio.Event()
        self.specialist_task: asyncio.Task | None = None

    async def request(self, msg: BusMessage, timeout: float) -> None:
        reservation = msg.context["_voice_handoff_reservation"]
        assert reservation.reserve() is True
        # A token arriving while the (normally async) prelaunch path is held
        # must not win the foreground race.
        await msg.context["_on_token"]("This must not be spoken.")
        self.specialist_task = asyncio.create_task(asyncio.Event().wait())
        reservation.commit(_HandoffJob())
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert self.specialist_task is not None
            assert not self.specialist_task.done()
            assert self.request_cancelled is not None
            self.request_cancelled.set()
            raise


class _FailingHandoffWs(_RecordingWs):
    """A transport failure must use the ordinary error path, not succeed."""

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)
        if frame["type"] == "handoff":
            raise ConnectionResetError("closing transport")
        await asyncio.sleep(0)
        self.write_completed.set()


@pytest.fixture
async def ws_app():
    telemetry_clock = iter((20.0, 20.250))
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
        monotonic=lambda: next(telemetry_clock),
    )
    app = web.Application()
    ch.register_routes(app)

    async with TestClient(TestServer(app)) as client:
        yield client, bus, memory, ch
    loop.cancel()


@pytest.fixture
async def signed_ws_app():
    secret = "route-secret"
    bus = MessageBus()
    for role in ("concierge", "butler"):
        agent = _StreamingAgent(bus, role)
        bus.register(role, agent.handle_message)
    loops = [
        asyncio.create_task(bus.run_agent_loop(role))
        for role in ("concierge", "butler")
    ]
    delivery = _DeliverySpy()
    channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret=secret,
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={
            "concierge": _FakeCfg(),
            "butler": _FakeCfg(),
            "text-only": _TextOnlyCfg(),
        },
        memory=AsyncMock(),
        idle_timeout=300,
        delivery_coordinator=delivery,
    )
    app = web.Application()
    channel.register_routes(app)
    signature = hmac.new(
        secret.encode(), b"", hashlib.sha256,
    ).hexdigest()
    async with TestClient(TestServer(app)) as client:
        yield client, channel, delivery, {
            "X-Webhook-Signature": signature,
        }
    for task in loops:
        task.cancel()


@pytest.fixture
async def unsigned_route_ws_app():
    bus = MessageBus()
    channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret="",
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeCfg()},
        memory=AsyncMock(),
        idle_timeout=300,
    )
    app = web.Application()
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client, channel


@pytest.mark.asyncio
class TestWSTurn:
    async def test_handoff_ends_only_the_foreground_request_after_its_frame(
        self,
    ):
        """A durable job owns the background task, not the old utterance."""
        bus = _HandoffingBus()
        channel = VoiceChannel(
            bus=bus, default_agent="concierge", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"concierge": _FakeCfg()}, memory=AsyncMock(),
            idle_timeout=300,
        )
        ws = _RecordingWs()

        await channel._run_ws_utterance(
            ws,
            {
                "agent_role": "concierge", "text": "please ask finance",
                "scope_id": "scope-1", "device_id": "kitchen",
            },
            "utterance-1",
            asyncio.get_running_loop().time() + 20,
        )

        assert ws.frames == [{
            "type": "handoff", "utterance_id": "utterance-1",
            "handoff_id": "handoff-1", "text": "I will ask Finance.",
        }]
        assert ws.write_completed.is_set()
        assert bus.request_cancelled.is_set()
        assert bus.specialist_task is not None
        assert not bus.specialist_task.done()
        bus.specialist_task.cancel()
        await asyncio.gather(bus.specialist_task, return_exceptions=True)

    async def test_handoff_reservation_releases_streaming_and_rejects_late_reserve(
        self,
    ):
        reservation = VoiceHandoffReservation()

        assert reservation.reserve() is True
        assert reservation.held is True
        reservation.release()
        assert reservation.held is False
        reservation.mark_speech_sent()
        assert reservation.reserve() is False

    async def test_failed_handoff_write_does_not_fake_a_terminal_success(self):
        bus = _HandoffingBus()
        channel = VoiceChannel(
            bus=bus, default_agent="concierge", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"concierge": _FakeCfg()}, memory=AsyncMock(),
            idle_timeout=300,
        )
        ws = _FailingHandoffWs()

        await channel._run_ws_utterance(
            ws,
            {
                "agent_role": "concierge", "text": "please ask finance",
                "scope_id": "scope-1", "device_id": "kitchen",
            },
            "utterance-1",
            asyncio.get_running_loop().time() + 20,
        )

        assert [frame["type"] for frame in ws.frames] == ["handoff", "error"]
        assert bus.request_cancelled.is_set()
        assert bus.specialist_task is not None
        assert not bus.specialist_task.done()
        bus.specialist_task.cancel()
        await asyncio.gather(bus.specialist_task, return_exceptions=True)

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

    async def test_first_real_block_logs_once_from_ingress_with_fake_clock(
        self, ws_app, caplog,
    ):
        client, _, _, _ = ws_app
        secret = "SECRET_WS_PROMPT"
        with caplog.at_level(logging.INFO, logger="channels.voice.channel"):
            async with client.ws_connect("/api/converse/ws") as ws:
                await ws.send_json({
                    "type": "utterance",
                    "utterance_id": "latency-ws",
                    "text": secret,
                    "agent_role": "butler",
                    "scope_id": "latency-ws",
                })
                async for message in ws:
                    if message.type != WSMsgType.TEXT:
                        break
                    if json.loads(message.data)["type"] == "done":
                        break

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "channels.voice.channel"
            and "voice_first_block" in record.getMessage()
        ]
        assert messages == [
            "voice_first_block role=butler transport=ws ms=250"
        ]
        assert secret not in caplog.text

    async def test_stt_start_is_pool_noop(self, ws_app):
        """v0.80.0 (spec A2): stt_start no longer touches the pool at all —
        the frame carries no agent_role, and VoiceSessionPool.ensure() now
        requires one (role-scoped keying, so two residents on one device
        can't collide on a session_key). Pool registration happens lazily
        on the utterance frame instead, which DOES carry agent_role.
        Sending stt_start twice remains a harmless no-op either way. The
        obsolete overlay prewarm has been removed — no profile() is called."""
        client, _, memory, channel = ws_app

        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({"type": "stt_start", "scope_id": "s"})
            await ws.send_json({"type": "stt_start", "scope_id": "s"})
            await asyncio.sleep(0.05)
            assert channel.pool.get("s", role="butler") is None
            # No profile() call — overlay not used for voice.
            assert memory.profile.await_count == 0

    async def test_role_aware_stt_start_ensures_exact_role_scope_only(
        self, signed_ws_app,
    ):
        client, channel, _, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({
                "type": "stt_start", "scope_id": "device-1",
                "agent_role": "concierge",
            })
            await ws.send_json({
                "type": "stt_start", "scope_id": "device-1",
                "agent_role": "butler",
            })
            await asyncio.sleep(0.05)

        assert channel.pool.get("device-1", role="concierge") is not None
        assert channel.pool.get("device-1", role="butler") is not None

    @pytest.mark.parametrize("frame", [
        {"type": "stt_start", "scope_id": "device-1"},
        {"type": "stt_start", "scope_id": "", "agent_role": "butler"},
        {"type": "stt_start", "scope_id": "device-1", "agent_role": "unknown"},
        {"type": "stt_start", "scope_id": "device-1", "agent_role": "text-only"},
    ])
    async def test_invalid_stt_start_is_a_noop(self, signed_ws_app, frame):
        client, channel, _, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json(frame)
            await asyncio.sleep(0.02)
        assert channel.pool._sessions == {}

    async def test_job_frame_without_utterance_id_reaches_delivery_first(
        self, signed_ws_app,
    ):
        client, _, delivery, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({
                "type": "job_claimed", "protocol": 2,
                "job_id": "job-1",
                "delivery_attempt_id": "attempt-1",
            })
            await asyncio.wait_for(delivery.called.wait(), timeout=1)
        connection, frame = delivery.calls[-1]
        assert isinstance(connection, VoiceWsConnection)
        assert frame["type"] == "job_claimed"

    async def test_unknown_frame_is_ignored_without_error_or_close(
        self, signed_ws_app,
    ):
        client, _, _, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({"type": "future_frame", "protocol": 77})
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.receive_json(), timeout=0.05)
            assert ws.closed is False

    async def test_handoff_received_before_route_registration_is_ignored(
        self, signed_ws_app,
    ):
        client, channel, delivery, headers = signed_ws_app
        dispatch = AsyncMock()
        channel._bus.handlers["butler"] = dispatch

        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({
                "type": "handoff_received",
                "protocol": 2,
                "utterance_id": "u-1",
                "handoff_id": "handoff-1",
                "text": "I will look into that.",
            })
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.receive_json(), timeout=0.05)

        assert channel.routes.get_connected("entry-1:concierge") is None
        assert delivery.calls == []
        dispatch.assert_not_awaited()

    async def test_non_object_json_frame_is_ignored_without_close(
        self, signed_ws_app,
    ):
        client, _, _, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json(["old", "integration", "frame"])
            await asyncio.sleep(0.05)
            assert ws.closed is False
            await ws.send_json({"type": "stage", "stage": "stt"})
            await asyncio.sleep(0.01)
            assert ws.closed is False

    async def test_authenticated_registration_binds_route(self, signed_ws_app):
        client, channel, _, headers = signed_ws_app
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({
                "type": "voice_route_register", "protocol": 2,
                "route_id": "entry-1", "agent_role": "concierge",
                "capabilities": [
                    "background_jobs", "satellite_announce", "voice_handoff",
                ],
            })
            assert await ws.receive_json() == {
                "type": "voice_route_registered", "protocol": 2,
                "accepted_capabilities": [
                    "background_jobs", "satellite_announce", "voice_handoff",
                ],
            }
            bound = channel.routes.get_connected("entry-1")
            assert bound is not None
            assert bound.role == "concierge"

    async def test_handler_failure_still_clears_connection_bound_writer(
        self, signed_ws_app,
    ):
        client, channel, delivery, headers = signed_ws_app

        async def fail_handle(_connection, _frame):
            raise RuntimeError("controlled delivery failure")

        delivery.handle = fail_handle
        async with client.ws_connect(
            "/api/converse/ws", headers=headers,
        ) as ws:
            await ws.send_json({
                "type": "voice_route_register", "protocol": 2,
                "route_id": "entry-1", "agent_role": "concierge",
                "capabilities": [
                    "background_jobs", "satellite_announce", "voice_handoff",
                ],
            })
            await ws.receive_json()
            assert channel.routes.get_connected("entry-1") is not None
            await ws.send_json({
                "type": "job_claimed", "protocol": 2,
                "job_id": "job-1",
                "delivery_attempt_id": "attempt-1",
            })
            await ws.receive()

        for _ in range(20):
            if channel.routes.get_connected("entry-1") is None:
                break
            await asyncio.sleep(0.01)
        assert channel.routes.get_connected("entry-1") is None

    async def test_empty_secret_never_accepts_background_capability(
        self, unsigned_route_ws_app,
    ):
        client, channel = unsigned_route_ws_app
        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({
                "type": "voice_route_register", "protocol": 2,
                "route_id": "entry-1", "agent_role": "butler",
                "capabilities": [
                    "background_jobs", "satellite_announce", "voice_handoff",
                ],
            })
            ack = await ws.receive_json()
            assert ack["accepted_capabilities"] == []
        assert channel.routes.get_connected("entry-1") is None

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

        async def stub_ok(ws, frame, uid, voice_deadline):
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
            async def stub_fail(ws_, frame, uid, voice_deadline):
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

    async def test_voice_tool_loop_emits_one_typed_error_without_payload_log(
        self, ws_app, caplog,
    ):
        client, bus, _, _ = ws_app
        secret = "SECRET_VOICE_TOOL_INPUT_WS"

        async def raise_voice_tool_loop(_msg, timeout=300):
            raise VoiceToolLoopError("validation_correction_exhausted")

        bus.request = raise_voice_tool_loop
        with caplog.at_level(logging.DEBUG):
            async with client.ws_connect("/api/converse/ws") as ws:
                await ws.send_json({
                    "type": "utterance",
                    "utterance_id": "guarded-ws",
                    "text": secret,
                    "agent_role": "butler",
                    "scope_id": "guarded-ws",
                })
                frames = [await ws.receive_json(timeout=2.0)]
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.receive_json(), timeout=0.05)

        errors = [frame for frame in frames if frame["type"] == "error"]
        assert len(errors) == 1
        assert errors[0] == {
            "type": "error",
            "utterance_id": "guarded-ws",
            "kind": "voice_tool_loop",
            "spoken": (
                "[apologetic] I couldn't resolve that cleanly. "
                "Try naming the device again?"
            ),
        }
        assert not any(frame["type"] == "done" for frame in frames)
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# Rate limiting — per-scope_id (spec 5.2 §8)
# ---------------------------------------------------------------------------


@pytest.fixture
async def voice_ws_app_with_limiter(request):
    from rate_limit import RateLimiter

    capacity = getattr(request, "param", 2)

    bus = MessageBus()
    roles = ("butler", "concierge")
    for role in roles:
        agent = _StreamingAgent(bus, role)
        bus.register(role, agent.handle_message)
    loop_tasks = [
        asyncio.create_task(bus.run_agent_loop(role)) for role in roles
    ]

    memory = AsyncMock()
    memory.ensure_session = AsyncMock(return_value=None)
    memory.get_context = AsyncMock(return_value="")
    memory.add_turn = AsyncMock(return_value=None)
    memory.profile = AsyncMock(return_value="")

    limiter = RateLimiter(capacity=capacity, window_s=60.0)

    channel = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret="",
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={role: _FakeCfg() for role in roles},
        memory=memory, idle_timeout=300,
        rate_limiter=limiter,
    )

    app = web.Application()
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client
    for task in loop_tasks:
        task.cancel()


@pytest.mark.asyncio
class TestRateLimit:
    @pytest.mark.parametrize("voice_ws_app_with_limiter", [1], indirect=True)
    async def test_same_scope_has_independent_role_buckets(
        self, voice_ws_app_with_limiter,
    ):
        client = voice_ws_app_with_limiter

        async with client.ws_connect("/api/converse/ws") as ws:
            async def run_turn(uid: str, role: str) -> dict:
                await ws.send_json({
                    "type": "utterance",
                    "utterance_id": uid,
                    "scope_id": "same-scope",
                    "agent_role": role,
                    "text": "hi",
                })
                while True:
                    frame = await ws.receive_json(timeout=2.0)
                    if (
                        frame.get("utterance_id") == uid
                        and frame.get("type") in {"done", "error"}
                    ):
                        return frame

            assert (await run_turn("u-butler-1", "butler"))["type"] == "done"
            assert (
                await run_turn("u-concierge-1", "concierge")
            )["type"] == "done"
            butler_second = await run_turn("u-butler-2", "butler")
            assert butler_second["type"] == "error"
            assert butler_second["kind"] == "rate_limit"

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
