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


@pytest.fixture
async def agent_error_voice_app():
    """Wire up a real Agent whose _process raises an UNKNOWN error, + a real
    VoiceChannel. Exercises the natural production path:
    Agent.handle_message catches → error_kind set → emit_error_line called →
    _error_sink fires event: error → SSE skips event: done.
    """
    from agent import Agent
    from config import AgentConfig, MemoryConfig, SessionConfig, ToolsConfig, TTSConfig
    from mcp_registry import McpServerRegistry
    from session_registry import SessionRegistry
    from channels import ChannelManager

    bus = MessageBus()

    cfg = AgentConfig(
        name="Tina",
        role="butler",
        model="claude-haiku-4-5",
        personality="Butler.",
        tools=ToolsConfig(),
        memory=MemoryConfig(token_budget=800, read_strategy="cached"),
        session=SessionConfig(strategy="pooled", idle_timeout=300),
        tts=TTSConfig(tag_dialect="square_brackets"),
        # RuntimeError classifies as UNKNOWN → this key is used.
        voice_errors={"unknown": "[apologetic] Natural-path Tina voice failure."},
    )

    channel_manager = ChannelManager()
    voice_channel = VoiceChannel(
        bus=bus,
        default_agent="butler",
        webhook_secret="",
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"butler": cfg},
        memory=_DummyMemory(),
        idle_timeout=300,
    )
    channel_manager.register(voice_channel)

    agent = Agent(
        config=cfg,
        memory=_DummyMemory(),
        session_registry=SessionRegistry("/tmp/_test_voice_sessions.json"),
        mcp_registry=McpServerRegistry(),
        channel_manager=channel_manager,
    )

    async def _raise(*args, **kwargs):
        raise RuntimeError("SDK-style failure")

    agent._process = _raise  # type: ignore[assignment]

    bus.register("butler", agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

    app = web.Application()
    voice_channel.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client
    loop_task.cancel()


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

    async def test_agent_sdk_error_routes_through_emit_error_line(
        self, agent_error_voice_app,
    ):
        """Spec §7 end-to-end: agent catches SDK error, VoiceChannel emits
        event: error with persona line, no event: done is sent."""
        client = agent_error_voice_app
        resp = await client.post(
            "/api/converse",
            json={"prompt": "hi", "agent_role": "butler", "scope_id": "s"},
        )
        assert resp.status == 200
        body = (await resp.read()).decode("utf-8")
        assert "event: error" in body
        assert "Natural-path Tina voice failure" in body
        # Crucially: done MUST NOT be emitted after error.
        assert "event: done" not in body
