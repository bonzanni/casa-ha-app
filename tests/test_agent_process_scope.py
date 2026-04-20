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

    @classmethod
    def reset(cls) -> None:
        cls.captured_options = None
        cls.response_text = "ok"

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
    reg.cache_stats = Mock(return_value=(0, 1))
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


class TestWritePath:
    async def test_classify_writes_to_argmax(self, monkeypatch):
        """Classify full exchange → write_scope = argmax."""
        from agent import Agent, ClaudeSDKClient
        from bus import BusMessage, MessageType

        memory = Mock()
        memory.ensure_session = AsyncMock()
        memory.get_context = AsyncMock(return_value="d")
        memory.add_turn = AsyncMock()

        # score() is called twice (read + write); make the WRITE call
        # favour finance by returning different dicts on 2nd call.
        reg = Mock()
        reg.filter_readable = Mock(return_value=["personal", "business", "finance", "house"])
        reg.score = Mock(side_effect=[
            {"personal": 1, "business": 1, "finance": 1, "house": 1},   # read
            {"personal": 0.1, "business": 0.1, "finance": 0.9, "house": 0.2},   # write
        ])
        reg.active_from_scores = Mock(return_value=["finance"])
        reg.argmax_scope = Mock(return_value="finance")
        reg.cache_stats = Mock(return_value=(0, 1))

        with patch("agent.ClaudeSDKClient", FakeClient):
            FakeClient.reset()
            FakeClient.response_text = "€450 to Van der Berg"

            agent = Agent(
                config=_make_agent_config(),
                memory=memory,
                session_registry=Mock(get=Mock(return_value=None),
                                      touch=AsyncMock(),
                                      register=AsyncMock()),
                mcp_registry=Mock(resolve=Mock(return_value={})),
                channel_manager=Mock(),
                scope_registry=reg,
            )

            await agent._process(BusMessage(
                type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
                content="plumber?", channel="telegram", context={"chat_id": "12345"},
            ))

        # Drain background add_turn.
        await asyncio.gather(*list(agent._bg_tasks), return_exceptions=True)

        memory.add_turn.assert_awaited_once()
        kwargs = memory.add_turn.await_args.kwargs
        assert kwargs["session_id"] == "telegram:12345:finance:assistant"

    async def test_write_restricted_to_owned_and_readable(self, monkeypatch):
        """Write-scope classifier sees only (scopes_owned ∩ readable)."""
        from agent import Agent, ClaudeSDKClient
        from bus import BusMessage, MessageType

        memory = Mock()
        memory.ensure_session = AsyncMock()
        memory.get_context = AsyncMock(return_value="")
        memory.add_turn = AsyncMock()

        reg = Mock()
        # Trust permits all readable scopes
        reg.filter_readable = Mock(return_value=["personal", "business", "finance", "house"])
        reg.score = Mock(return_value={"personal": 1, "business": 1, "finance": 1, "house": 1})
        reg.active_from_scores = Mock(return_value=["personal"])
        reg.argmax_scope = Mock(return_value="personal")
        reg.cache_stats = Mock(return_value=(0, 1))

        with patch("agent.ClaudeSDKClient", FakeClient):
            FakeClient.reset()
            FakeClient.response_text = "ok"

            agent = Agent(
                config=_make_agent_config(),   # owned=[personal, business, finance]
                memory=memory,
                session_registry=Mock(get=Mock(return_value=None),
                                      touch=AsyncMock(),
                                      register=AsyncMock()),
                mcp_registry=Mock(resolve=Mock(return_value={})),
                channel_manager=Mock(),
                scope_registry=reg,
            )

            await agent._process(BusMessage(
                type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
                content="hi", channel="telegram", context={"chat_id": "12345"},
            ))
        await asyncio.gather(*list(agent._bg_tasks), return_exceptions=True)

        # The 2nd score() call (for write) was invoked with owned∩readable:
        # owned = [personal, business, finance]; readable = [personal, business, finance, house]
        # → intersection = [personal, business, finance]
        write_call = reg.score.call_args_list[1]
        # Signature: score(text, scopes)
        assert sorted(write_call.args[1]) == ["business", "finance", "personal"]

    async def test_empty_assistant_text_does_not_write(self, monkeypatch):
        """SDK returns empty string → no add_turn call (gate remains on response_text)."""
        from agent import Agent, ClaudeSDKClient
        from bus import BusMessage, MessageType

        memory = Mock()
        memory.ensure_session = AsyncMock()
        memory.get_context = AsyncMock(return_value="")
        memory.add_turn = AsyncMock()

        reg = Mock()
        reg.filter_readable = Mock(return_value=["personal", "house"])
        reg.score = Mock(return_value={"personal": 1.0, "house": 0.1})
        reg.active_from_scores = Mock(return_value=["personal"])
        reg.argmax_scope = Mock(return_value="personal")
        reg.cache_stats = Mock(return_value=(0, 1))

        with patch("agent.ClaudeSDKClient", FakeClient):
            FakeClient.reset()
            FakeClient.response_text = ""  # empty

            agent = Agent(
                config=_make_agent_config(),
                memory=memory,
                session_registry=Mock(get=Mock(return_value=None),
                                      touch=AsyncMock(),
                                      register=AsyncMock()),
                mcp_registry=Mock(resolve=Mock(return_value={})),
                channel_manager=Mock(),
                scope_registry=reg,
            )

            await agent._process(BusMessage(
                type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
                content="anything", channel="telegram", context={"chat_id": "12345"},
            ))
        await asyncio.gather(*list(agent._bg_tasks), return_exceptions=True)

        # Empty assistant text ⇒ no write (spec §14.3 acceptable either way;
        # v0.8.0 preserves today's behaviour — gate on response_text truthy).
        assert memory.add_turn.await_count == 0

    async def test_single_owned_scope_skips_write_classify(self, monkeypatch):
        """Butler (voice) with scopes_owned=[house] should NOT call score()
        on the write path — argmax over a 1-element list is trivially that
        element, so the ONNX forward pass is pure waste."""
        from agent import Agent, ClaudeSDKClient
        from bus import BusMessage, MessageType
        from config import (
            AgentConfig, CharacterConfig, MemoryConfig, SessionConfig,
            ToolsConfig, VoiceConfig, ResponseShapeConfig,
        )

        memory = Mock()
        memory.ensure_session = AsyncMock()
        memory.get_context = AsyncMock(return_value="")
        memory.add_turn = AsyncMock()

        # Butler config — owns only house.
        cfg = AgentConfig(role="butler")
        cfg.character = CharacterConfig(name="Tina", archetype="", card="", prompt="SYS")
        cfg.voice = VoiceConfig()
        cfg.response_shape = ResponseShapeConfig()
        cfg.system_prompt = "SYS"
        cfg.tools = ToolsConfig(allowed=["Read"], permission_mode="acceptEdits")
        cfg.memory = MemoryConfig(
            token_budget=800, read_strategy="cached",
            scopes_owned=["house"], scopes_readable=["house"],
            default_scope="house",
        )
        cfg.session = SessionConfig(strategy="pooled", idle_timeout=300)
        cfg.channels = ["voice"]
        cfg.model = "haiku"

        reg = Mock()
        reg.filter_readable = Mock(return_value=["house"])
        reg.score = Mock(return_value={"house": 0.9})
        reg.active_from_scores = Mock(return_value=["house"])
        reg.argmax_scope = Mock(return_value="house")
        reg.cache_stats = Mock(return_value=(0, 1))

        with patch("agent.ClaudeSDKClient", FakeClient):
            FakeClient.reset()
            FakeClient.response_text = "done"

            agent = Agent(
                config=cfg, memory=memory,
                session_registry=Mock(get=Mock(return_value=None),
                                      touch=AsyncMock(),
                                      register=AsyncMock()),
                mcp_registry=Mock(resolve=Mock(return_value={})),
                channel_manager=Mock(), scope_registry=reg,
            )

            await agent._process(BusMessage(
                type=MessageType.CHANNEL_IN, source="voice", target="butler",
                content="lights off", channel="voice", context={"chat_id": "lr"},
            ))
        await asyncio.gather(*list(agent._bg_tasks), return_exceptions=True)

        # Only the READ-path score() call; no write-side score()
        assert reg.score.call_count == 1
        memory.add_turn.assert_awaited_once()
        write_sid = memory.add_turn.await_args.kwargs["session_id"]
        assert write_sid == "voice:lr:house:butler"

    async def test_write_skipped_when_owned_and_readable_empty(self, monkeypatch):
        """Trust-bypass regression: if the channel's trust tier filters out
        every scope the agent owns, the write path must NOT fall back to
        default_scope (which would leak the exchange into a scope the
        channel can't see). Skip the write entirely."""
        from agent import Agent, ClaudeSDKClient
        from bus import BusMessage, MessageType

        memory = Mock()
        memory.ensure_session = AsyncMock()
        memory.get_context = AsyncMock(return_value="")
        memory.add_turn = AsyncMock()

        # Webhook (external-authenticated) against assistant:
        # readable reduced to [house]; owned = [personal, business, finance];
        # intersection is empty.
        reg = Mock()
        reg.filter_readable = Mock(return_value=["house"])
        reg.score = Mock(return_value={"house": 0.1})
        reg.active_from_scores = Mock(return_value=["house"])
        reg.argmax_scope = Mock(return_value="house")
        reg.cache_stats = Mock(return_value=(0, 1))

        with patch("agent.ClaudeSDKClient", FakeClient):
            FakeClient.reset()
            FakeClient.response_text = "reply from assistant"

            agent = Agent(
                config=_make_agent_config(),   # owned=[personal, business, finance]
                memory=memory,
                session_registry=Mock(get=Mock(return_value=None),
                                      touch=AsyncMock(),
                                      register=AsyncMock()),
                mcp_registry=Mock(resolve=Mock(return_value={})),
                channel_manager=Mock(),
                scope_registry=reg,
            )

            await agent._process(BusMessage(
                type=MessageType.CHANNEL_IN, source="webhook", target="assistant",
                content="probe", channel="webhook", context={"chat_id": "ext1"},
            ))
        await asyncio.gather(*list(agent._bg_tasks), return_exceptions=True)

        # owned∩readable is empty → no write. The classifier score() call
        # for the write step should also be skipped (only the read-side
        # score() call should appear).
        assert memory.add_turn.await_count == 0
        assert reg.score.call_count == 1  # read-only; no write-side score


class TestObservability:
    async def test_scope_route_log_emitted(self, monkeypatch, caplog):
        import logging
        from agent import Agent, ClaudeSDKClient
        from bus import BusMessage, MessageType

        memory = Mock()
        memory.ensure_session = AsyncMock()
        memory.get_context = AsyncMock(return_value="d")
        memory.add_turn = AsyncMock()

        reg = Mock()
        reg.filter_readable = Mock(return_value=["personal", "finance", "house"])
        reg.score = Mock(return_value={"personal": 0.1, "finance": 0.9, "house": 0.5})
        reg.active_from_scores = Mock(return_value=["finance", "house"])
        reg.cache_stats = Mock(return_value=(0, 1))
        reg.argmax_scope = Mock(return_value="finance")

        with patch("agent.ClaudeSDKClient", FakeClient):
            FakeClient.reset()
            FakeClient.response_text = "reply"

            agent = Agent(
                config=_make_agent_config(),
                memory=memory,
                session_registry=Mock(get=Mock(return_value=None),
                                      touch=AsyncMock(),
                                      register=AsyncMock()),
                mcp_registry=Mock(resolve=Mock(return_value={})),
                channel_manager=Mock(),
                scope_registry=reg,
            )

            caplog.set_level(logging.INFO, logger="agent")
            await agent._process(BusMessage(
                type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
                content="x", channel="telegram", context={"chat_id": "12345"},
            ))
        await asyncio.gather(*list(agent._bg_tasks), return_exceptions=True)

        # Expected shape:
        # scope_route role=assistant channel=telegram active=[finance,house] write=finance (t=<N>ms)
        routes = [r for r in caplog.records if "scope_route" in r.message]
        assert len(routes) == 1, f"expected 1 scope_route log, got {[r.message for r in routes]}"
        m = routes[0].message
        assert "role=assistant" in m
        assert "channel=telegram" in m
        assert "active=[finance,house]" in m or "active=[house,finance]" in m
        assert "write=finance" in m
        assert "t=" in m and "ms" in m
