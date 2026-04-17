"""End-to-end Agent._process loop against SqliteMemoryProvider.

Uses a real :memory: DB + the same stubbed SDK pattern as
tests/test_agent_process.py. Verifies messages are actually persisted
after the background add_turn task runs.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from memory import SqliteMemoryProvider
from session_registry import SessionRegistry

from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

pytestmark = pytest.mark.asyncio


def _mk_text_block(text: str) -> _SDKTextBlock:
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


class FakeClient:
    """Minimal ClaudeSDKClient substitute that yields a fixed reply."""

    captured_options = None
    response_text: str = "hi, Nicola"

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
    memory,
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


def _msg(channel: str, chat_id: str, text: str) -> BusMessage:
    return BusMessage(
        type=MessageType.CHANNEL_IN,
        source="telegram" if channel == "telegram" else channel,
        target="assistant",
        content=text,
        channel=channel,
        context={"chat_id": chat_id},
    )


async def _drain(agent: Agent | None = None) -> None:
    # Let the background add_turn task finish. asyncio.to_thread roundtrips
    # through the default executor, so sleep(0) alone isn't enough — we
    # need real wall-clock yields and, when available, to wait on the
    # agent's own tracked background tasks.
    if agent is not None and agent._bg_tasks:
        await asyncio.wait(
            set(agent._bg_tasks), timeout=2.0,
        )
    for _ in range(10):
        await asyncio.sleep(0)


async def test_telegram_turn_persists_nicola_and_assistant_rows(tmp_path):
    memory = SqliteMemoryProvider(":memory:")
    agent = _make_agent(memory, tmp_path, role="assistant")

    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "nicola", "hello"))
    await _drain(agent)

    rows = memory._conn.execute(
        "SELECT peer_name, content FROM messages "
        "WHERE session_id = ? ORDER BY id ASC",
        ("telegram:nicola:assistant",),
    ).fetchall()
    assert rows == [("nicola", "hello"), ("assistant", "hi, Nicola")]


async def test_voice_turn_attributes_to_voice_speaker(tmp_path):
    memory = SqliteMemoryProvider(":memory:")
    agent = _make_agent(memory, tmp_path, role="butler")

    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("voice", "livingroom", "lights on"))
    await _drain(agent)

    rows = memory._conn.execute(
        "SELECT peer_name, content FROM messages "
        "WHERE session_id = ? ORDER BY id ASC",
        ("voice:livingroom:butler",),
    ).fetchall()
    assert rows == [
        ("voice_speaker", "lights on"),
        ("butler", "hi, Nicola"),
    ]


async def test_second_turn_sees_first_turn_in_memory_context(tmp_path):
    memory = SqliteMemoryProvider(":memory:")
    agent = _make_agent(memory, tmp_path, role="assistant")

    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "n", "first question"))
    await _drain(agent)

    ctx = await memory.get_context(
        "telegram:n:assistant", "assistant", tokens=4000,
    )
    assert "## Recent exchanges" in ctx
    assert "[nicola] first question" in ctx
