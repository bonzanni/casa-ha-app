"""Spec §3.1 — SSE transport of VoiceChannel.

Uses aiohttp TestClient + a stub bus request/response pair to drive a
turn end-to-end over HTTP without touching the SDK.
"""

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bus import BusMessage, MessageBus, MessageType
from channels.voice.channel import VoiceChannel


class StubAgent:
    """Synthetic agent: streams two tokens via on_token, returns a full text."""

    def __init__(self, bus: MessageBus, role: str) -> None:
        self._bus = bus
        self._role = role

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        on_token = msg.context.get("_on_token")
        if on_token:
            await on_token("[confident] Done.")
            await on_token("[confident] Done. Kitchen lights off.")
        return BusMessage(
            type=MessageType.RESPONSE,
            source=self._role,
            target=msg.source,
            content="[confident] Done. Kitchen lights off.",
            reply_to=msg.id,
            channel=msg.channel,
            context=msg.context,
        )


class _FakeAgentConfig:
    class tts:
        tag_dialect = "square_brackets"
    memory = type("M", (), {"token_budget": 800})()
    role = "butler"
    voice_errors: dict[str, str] = {}


class _DummyMemory:
    async def ensure_session(self, *a, **kw): return None
    async def get_context(self, *a, **kw): return ""
    async def add_turn(self, *a, **kw): return None


@pytest.fixture
async def voice_app():
    bus = MessageBus()
    agent = StubAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

    channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret="",
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeAgentConfig()},
        memory=_DummyMemory(),
        idle_timeout=300,
    )

    app = web.Application()
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client, bus
    loop_task.cancel()


@pytest.fixture
async def broken_voice_app(monkeypatch):
    """Fixture with monkeypatched bus.request that raises to trigger error handler."""
    bus = MessageBus()
    bus.register("butler", None)

    butler_cfg = _FakeAgentConfig()
    butler_cfg.voice_errors = {"unknown": "[flat] Tina-voice failure."}

    channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret="",
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": butler_cfg},
        memory=_DummyMemory(),
        idle_timeout=300,
    )

    async def fake_request(msg, timeout=300):
        raise RuntimeError("simulated SDK failure")

    # Replace the channel's bus.request method to raise unconditionally
    monkeypatch.setattr(channel._bus, "request", fake_request)

    app = web.Application()
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client, bus


@pytest.mark.asyncio
class TestSSE:
    async def test_full_turn_emits_blocks_then_done(self, voice_app):
        client, _ = voice_app
        resp = await client.post(
            "/api/converse",
            json={
                "prompt": "turn off kitchen lights",
                "agent_role": "butler",
                "scope_id": "user-xyz",
                "channel": "voice",
                "context": {"device_id": "kitchen", "language": "en"},
            },
        )
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")

        frames = []
        async for line in resp.content:
            s = line.decode("utf-8").rstrip("\r\n")
            if s.startswith("event:"):
                evt = s.split(":", 1)[1].strip()
                frames.append({"event": evt})
            elif s.startswith("data:"):
                frames[-1]["data"] = json.loads(s.split(":", 1)[1].strip())

        events = [f["event"] for f in frames]
        assert "block" in events
        assert events[-1] == "done"

    async def test_unknown_agent_role_404(self, voice_app):
        client, _ = voice_app
        resp = await client.post(
            "/api/converse",
            json={"prompt": "x", "agent_role": "ghost", "scope_id": "s"},
        )
        assert resp.status == 404

    async def test_missing_prompt_400(self, voice_app):
        client, _ = voice_app
        resp = await client.post("/api/converse", json={"scope_id": "s"})
        assert resp.status == 400

    async def test_error_frame_carries_persona_line(self, broken_voice_app):
        client, _ = broken_voice_app
        resp = await client.post(
            "/api/converse",
            json={"prompt": "hi", "agent_role": "butler", "scope_id": "s"},
        )
        assert resp.status == 200
        body = await resp.read()
        text = body.decode("utf-8")
        assert "event: error" in text
        assert "Tina-voice failure" in text
