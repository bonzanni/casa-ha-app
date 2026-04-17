"""Integration tests for Agent._process — memory wiring + channel_context."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from memory import MemoryProvider
from session_registry import SessionRegistry

from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

pytestmark = pytest.mark.asyncio


def _mk_text_block(text: str) -> _SDKTextBlock:
    """Instantiate whatever TextBlock shape the installed SDK uses."""
    try:
        return _SDKTextBlock(text=text)
    except TypeError:
        return _SDKTextBlock(text)  # type: ignore[call-arg]


def _mk_assistant(text: str) -> _SDKAssistantMessage:
    block = _mk_text_block(text)
    try:
        return _SDKAssistantMessage(content=[block])
    except TypeError:
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = [block]  # type: ignore[attr-defined]
        return m


def _mk_result(sid: str) -> _SDKResultMessage:
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid  # type: ignore[attr-defined]
    return m


class FakeMemory(MemoryProvider):
    def __init__(self, context: str = "") -> None:
        self.context = context
        self.ensure: list[tuple] = []
        self.get: list[tuple] = []
        self.add: list[tuple] = []

    async def ensure_session(self, session_id, agent_role, user_peer="nicola"):
        self.ensure.append((session_id, agent_role, user_peer))

    async def get_context(
        self, session_id, agent_role, tokens,
        search_query=None, user_peer="nicola",
    ):
        self.get.append(
            (session_id, agent_role, tokens, search_query, user_peer)
        )
        return self.context

    async def add_turn(
        self, session_id, agent_role, user_text, assistant_text,
        user_peer="nicola",
    ):
        self.add.append(
            (session_id, agent_role, user_text, assistant_text, user_peer)
        )


class FakeClient:
    """Minimal ClaudeSDKClient substitute."""

    captured_options = None
    response_text: str = "pong"

    def __init__(self, options):
        FakeClient.captured_options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def query(self, text):
        self._last = text

    async def receive_response(self):
        yield _mk_assistant(FakeClient.response_text)
        yield _mk_result("sdk-sid-1")


def _make_agent(
    memory: MemoryProvider,
    tmp_path,
    role: str = "assistant",
) -> Agent:
    cfg = AgentConfig(
        name="Test",
        role=role,
        model="claude-sonnet-4-6",
        personality="You are helpful.",
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
    )
    return Agent(
        config=cfg,
        memory=memory,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )


def _msg(channel: str, chat_id: str, text: str = "ping") -> BusMessage:
    return BusMessage(
        type=MessageType.CHANNEL_IN,
        source="telegram" if channel == "telegram" else channel,
        target="assistant",
        content=text,
        channel=channel,
        context={"chat_id": chat_id},
    )


async def test_session_id_is_channel_plus_role(tmp_path):
    mem = FakeMemory()
    agent = _make_agent(mem, tmp_path, role="assistant")
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    assert mem.ensure[0][0] == "telegram:123:assistant"
    assert mem.get[0][0] == "telegram:123:assistant"
    for _ in range(5):
        await asyncio.sleep(0)
    assert mem.add[0][0] == "telegram:123:assistant"


async def test_voice_channel_uses_voice_speaker_peer(tmp_path):
    mem = FakeMemory()
    agent = _make_agent(mem, tmp_path, role="butler")
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("voice", "lr", "lights on"))

    assert mem.ensure[0][2] == "voice_speaker"
    assert mem.get[0][4] == "voice_speaker"
    for _ in range(5):
        await asyncio.sleep(0)
    assert mem.add[0][4] == "voice_speaker"


async def test_telegram_channel_uses_nicola_peer(tmp_path):
    mem = FakeMemory()
    agent = _make_agent(mem, tmp_path, role="assistant")
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    assert mem.get[0][4] == "nicola"


async def test_system_prompt_contains_channel_context(tmp_path):
    mem = FakeMemory(context="")
    agent = _make_agent(mem, tmp_path, role="assistant")
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    prompt = FakeClient.captured_options.system_prompt
    assert "<channel_context>" in prompt
    assert "channel: telegram" in prompt
    assert "trust: authenticated (Nicola)" in prompt
    assert "</channel_context>" in prompt


async def test_system_prompt_memory_context_only_when_nonempty(tmp_path):
    mem = FakeMemory(context="")
    agent = _make_agent(mem, tmp_path, role="assistant")
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))
    assert "<memory_context>" not in FakeClient.captured_options.system_prompt

    mem2 = FakeMemory(context="## Recent\n[nicola] hi")
    agent2 = _make_agent(mem2, tmp_path, role="assistant")
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent2._process(_msg("telegram", "123", "hi"))
    prompt2 = FakeClient.captured_options.system_prompt
    assert "<memory_context>" in prompt2
    assert "[nicola] hi" in prompt2


async def test_add_turn_runs_as_background_task(tmp_path):
    mem = FakeMemory()
    agent = _make_agent(mem, tmp_path, role="assistant")
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    assert FakeClient.response_text == "pong"
    for _ in range(5):
        await asyncio.sleep(0)
    assert mem.add[0][2] == "hi"
    assert mem.add[0][3] == "pong"


async def test_memory_failure_does_not_break_response(tmp_path, caplog):
    import logging

    class BrokenMemory(FakeMemory):
        async def get_context(self, *a, **kw):
            raise RuntimeError("honcho down")

    mem = BrokenMemory()
    agent = _make_agent(mem, tmp_path, role="assistant")
    with patch("agent.ClaudeSDKClient", FakeClient):
        with caplog.at_level(logging.WARNING):
            out = await agent._process(_msg("telegram", "123", "hi"))
    assert out == "pong"
    assert any("memory" in r.message.lower() for r in caplog.records)
    prompt = FakeClient.captured_options.system_prompt
    assert "<memory_context>" not in prompt
    assert "<channel_context>" in prompt


async def test_add_turn_failure_logs_warning(tmp_path, caplog):
    import logging

    class BrokenAdd(FakeMemory):
        async def add_turn(self, *a, **kw):
            raise RuntimeError("honcho write down")

    mem = BrokenAdd()
    agent = _make_agent(mem, tmp_path, role="assistant")
    with patch("agent.ClaudeSDKClient", FakeClient):
        with caplog.at_level(logging.WARNING):
            out = await agent._process(_msg("telegram", "123", "hi"))
    assert out == "pong"
    for _ in range(5):
        await asyncio.sleep(0)
    assert any(
        "add_turn" in r.message.lower() for r in caplog.records
    )
