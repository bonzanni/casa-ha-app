"""End-to-end Agent._process loop against SqliteMemoryProvider.

Uses a real :memory: DB + the same stubbed SDK pattern as
tests/test_agent_process.py.

Task 7 (spec §4.2): the per-turn `add_turn` write is retired in favour of
session-granularity saves. The agent no longer persists rows on each turn;
the transcript is retained at session end via ``save_session`` (which needs a
real SDK transcript, out of scope for this unit fake). These tests assert that
NO rows are written to the provider per turn.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
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
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(
            token_budget=1000,
            read_strategy="per_turn",
        ),
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


def _row_count(memory) -> int:
    return memory._conn.execute(
        "SELECT COUNT(*) FROM messages",
    ).fetchone()[0]


async def test_telegram_turn_writes_no_rows(tmp_path):
    memory = SqliteMemoryProvider(":memory:")
    agent = _make_agent(memory, tmp_path, role="assistant")

    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "nicola", "hello"))
    await _drain(agent)

    # Per-turn write retired: no rows persisted by the turn.
    assert _row_count(memory) == 0
    # Registry entry exists (session registered for later reaper save).
    entry = agent._session_registry.get("telegram-nicola")
    assert entry is not None


async def test_voice_turn_writes_no_rows(tmp_path):
    memory = SqliteMemoryProvider(":memory:")
    agent = _make_agent(memory, tmp_path, role="butler")

    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("voice", "livingroom", "lights on"))
    await _drain(agent)

    assert _row_count(memory) == 0
    entry = agent._session_registry.get("voice-livingroom")
    assert entry is not None


async def test_turn_does_not_persist_to_provider(tmp_path):
    """The agent no longer writes turns to the MemoryProvider; a follow-up
    get_context therefore finds nothing the turn put there (the SDK session
    owns short-term recency now, and long-term retain happens at session
    end via save_session)."""
    memory = SqliteMemoryProvider(":memory:")
    agent = _make_agent(memory, tmp_path, role="assistant")

    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "n", "first question"))
    await _drain(agent)

    ctx = await memory.get_context(
        "telegram-n-personal-assistant", tokens=4000,
    )
    assert "first question" not in ctx
    assert _row_count(memory) == 0
