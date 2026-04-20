"""Tests for scope-aware read/write in Agent._process (3.2)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch, Mock, AsyncMock

import pytest

from agent import Agent
from bus import BusMessage, MessageType
from config import (
    AgentConfig, CharacterConfig, MemoryConfig, SessionConfig,
    ToolsConfig, VoiceConfig, ResponseShapeConfig,
)

from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# SDK helpers (mirror of test_agent_process.py)
# ---------------------------------------------------------------------------


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
    """Minimal ClaudeSDKClient substitute."""

    captured_options = None
    response_text: str = "ok"

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


# ---------------------------------------------------------------------------
# Config + registry helpers
# ---------------------------------------------------------------------------


def _make_agent_config(role="assistant", default_scope="personal"):
    cfg = AgentConfig(
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="SYS",
        character=CharacterConfig(name="Ellen"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(
            token_budget=4000,
            read_strategy="per_turn",
            scopes_owned=["personal", "business", "finance"],
            scopes_readable=["personal", "business", "finance", "house"],
            default_scope=default_scope,
        ),
    )
    return cfg


def _make_scope_registry(*, readable_out, active_out, argmax_out):
    """Synthesize a mocked ScopeRegistry with predetermined behaviour."""
    reg = Mock()
    reg.filter_readable = Mock(return_value=readable_out)
    reg.score = Mock(return_value={s: 1.0 for s in readable_out})
    reg.active_from_scores = Mock(return_value=active_out)
    reg.argmax_scope = Mock(return_value=argmax_out)
    return reg


def _make_agent(cfg, memory, scope_registry):
    from session_registry import SessionRegistry
    from mcp_registry import McpServerRegistry
    from channels import ChannelManager
    return Agent(
        config=cfg,
        memory=memory,
        session_registry=Mock(
            get=Mock(return_value=None),
            touch=AsyncMock(),
            register=AsyncMock(),
        ),
        mcp_registry=Mock(resolve=Mock(return_value={})),
        channel_manager=Mock(),
        scope_registry=scope_registry,
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReadPath:
    async def test_fan_out_one_scope(self):
        """Active=[finance] → exactly one get_context call to a finance session."""
        memory = Mock()
        memory.ensure_session = AsyncMock()
        memory.get_context = AsyncMock(return_value="finance digest")
        memory.add_turn = AsyncMock()

        reg = _make_scope_registry(
            readable_out=["personal", "business", "finance", "house"],
            active_out=["finance"],
            argmax_out="finance",
        )

        cfg = _make_agent_config(default_scope="personal")
        agent = _make_agent(cfg, memory, reg)

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "12345", "how much for the plumber?"))

        assert memory.get_context.call_count == 1
        called_sid = memory.get_context.call_args.kwargs["session_id"]
        assert called_sid == "telegram:12345:finance:assistant"

    async def test_fan_out_two_scopes_parallel(self):
        """Active=[finance, house] → two get_context calls, one per scope."""
        memory = Mock()
        memory.ensure_session = AsyncMock()
        memory.get_context = AsyncMock(return_value="d")
        memory.add_turn = AsyncMock()

        reg = _make_scope_registry(
            readable_out=["personal", "business", "finance", "house"],
            active_out=["finance", "house"],
            argmax_out="finance",
        )

        cfg = _make_agent_config(default_scope="personal")
        agent = _make_agent(cfg, memory, reg)

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "12345", "x"))

        session_ids_called = [
            c.kwargs["session_id"] for c in memory.get_context.call_args_list
        ]
        assert sorted(session_ids_called) == [
            "telegram:12345:finance:assistant",
            "telegram:12345:house:assistant",
        ]

    async def test_trust_filters_unreadable_scopes(self):
        """filter_readable called with (scopes_readable, channel_trust)."""
        memory = Mock()
        memory.ensure_session = AsyncMock()
        memory.get_context = AsyncMock(return_value="")
        memory.add_turn = AsyncMock()

        reg = _make_scope_registry(
            readable_out=["house"],  # trust reduces readable to just house
            active_out=["house"],
            argmax_out="house",
        )

        cfg = _make_agent_config(default_scope="personal")
        agent = _make_agent(cfg, memory, reg)

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("voice", "v1", "turn off lights"))

        args, kwargs = reg.filter_readable.call_args
        assert args[0] == ["personal", "business", "finance", "house"]
        assert args[1] == "household-shared"

    async def test_write_uses_default_scope(self):
        """Interim write path uses default_scope session ID."""
        memory = Mock()
        memory.ensure_session = AsyncMock()
        memory.get_context = AsyncMock(return_value="")
        memory.add_turn = AsyncMock()

        reg = _make_scope_registry(
            readable_out=["personal"],
            active_out=["personal"],
            argmax_out="personal",
        )

        cfg = _make_agent_config(default_scope="personal")
        agent = _make_agent(cfg, memory, reg)

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "12345", "hello"))

        # Drain background tasks
        for _ in range(5):
            await asyncio.sleep(0)

        assert memory.add_turn.call_count == 1
        write_sid = memory.add_turn.call_args.kwargs["session_id"]
        assert write_sid == "telegram:12345:personal:assistant"

    async def test_memory_context_includes_scope_attribute(self):
        """When digest is non-empty, system prompt has scope= attribute."""
        memory = Mock()
        memory.ensure_session = AsyncMock()
        memory.get_context = AsyncMock(return_value="some context data")
        memory.add_turn = AsyncMock()

        reg = _make_scope_registry(
            readable_out=["personal"],
            active_out=["personal"],
            argmax_out="personal",
        )

        cfg = _make_agent_config(default_scope="personal")
        agent = _make_agent(cfg, memory, reg)

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "12345", "hello"))

        prompt = FakeClient.captured_options.system_prompt
        assert '<memory_context scope="personal">' in prompt
        assert "some context data" in prompt
