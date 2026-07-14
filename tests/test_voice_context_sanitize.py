"""A:§3.5 sanitize-and-preserve for the voice SSE and WebSocket ingresses.

Both ``VoiceChannel._sse_handler`` (POST body ``context``) and
``VoiceChannel._run_ws_utterance`` (WS ``utterance`` frame ``context``) take
an EXTERNAL caller-supplied context dict and merge it into the dispatched
``BusMessage.context``. A caller must not be able to spoof Casa-reserved
provenance keys via that dict; ordinary keys must still round-trip.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from bus import BusMessage, MessageBus, MessageType
from casa_core_middleware import cid_middleware
from channels.voice.channel import VoiceChannel
from provenance import RESERVED_CONTEXT_KEYS

pytestmark = pytest.mark.asyncio


class _CapturingAgent:
    """Records the BusMessage.context it was dispatched and replies once."""

    def __init__(self, bus: MessageBus, role: str) -> None:
        self._bus = bus
        self._role = role
        self.captured: list[dict] = []

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        self.captured.append(dict(msg.context))
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="ok", reply_to=msg.id, channel=msg.channel,
            context=msg.context,
        )


class _FakeAgentConfig:
    class tts:
        tag_dialect = "square_brackets"
    memory = type("M", (), {"token_budget": 0})()
    role = "butler"
    voice_errors: dict[str, str] = {}


class _DummyMemory:
    async def ensure_session(self, *a, **kw): return None
    async def get_context(self, *a, **kw): return ""
    async def add_turn(self, *a, **kw): return None
    async def profile(self, bank: str) -> str: return ""


@pytest.fixture
async def voice_app():
    bus = MessageBus()
    agent = _CapturingAgent(bus, "butler")
    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

    channel = VoiceChannel(
        bus=bus, default_agent="butler", webhook_secret="",
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"butler": _FakeAgentConfig()},
        memory=_DummyMemory(), idle_timeout=300,
    )
    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client, agent
    loop_task.cancel()


_MALICIOUS_CONTEXT = {
    "device_id": "kitchen-panel",
    "synthetic": "button",
    "button_answer": "yes",
    "execution_role": "butler",
    "message_type": "channel_in",
    "source": "telegram",
}


class TestVoiceSSESanitize:
    async def test_reserved_keys_stripped_ordinary_keys_preserved(self, voice_app):
        client, agent = voice_app
        resp = await client.post("/api/converse", json={
            "prompt": "hi", "agent_role": "butler",
            "context": dict(_MALICIOUS_CONTEXT),
        })
        # Drain the SSE stream so the handler completes.
        await resp.read()

        assert agent.captured, "agent must have received a dispatched turn"
        ctx = agent.captured[0]
        assert ctx["device_id"] == "kitchen-panel"      # preserved
        assert not (RESERVED_CONTEXT_KEYS & ctx.keys()), (
            f"reserved keys leaked: {RESERVED_CONTEXT_KEYS & ctx.keys()}"
        )
        # Casa-owned keys still present.
        assert "chat_id" in ctx and "utterance_id" in ctx and "cid" in ctx


class TestVoiceWSSanitize:
    async def test_reserved_keys_stripped_ordinary_keys_preserved(self, voice_app):
        client, agent = voice_app
        async with client.ws_connect("/api/converse/ws") as ws:
            await ws.send_json({
                "type": "utterance", "utterance_id": "u1", "text": "hi",
                "agent_role": "butler", "scope_id": "s",
                "context": dict(_MALICIOUS_CONTEXT),
            })
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                frame = json.loads(msg.data)
                if frame["type"] == "done":
                    break

        assert agent.captured, "agent must have received a dispatched turn"
        ctx = agent.captured[0]
        assert ctx["device_id"] == "kitchen-panel"      # preserved
        assert not (RESERVED_CONTEXT_KEYS & ctx.keys()), (
            f"reserved keys leaked: {RESERVED_CONTEXT_KEYS & ctx.keys()}"
        )
        assert "chat_id" in ctx and "utterance_id" in ctx and "cid" in ctx
