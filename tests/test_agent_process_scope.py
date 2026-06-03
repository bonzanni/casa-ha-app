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

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


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


class FakeSemanticMemory:
    """SemanticMemory double for the §4.3 read path (profile + recall).

    Not a SemanticMemory subclass — _process only calls .profile()/.recall(),
    so a duck-typed recorder suffices and keeps these scope-routing tests
    focused on the read contract."""

    def __init__(self, overlay: str = "", facts: str = "") -> None:
        self._overlay = overlay
        self._facts = facts
        self.profile_calls: list[str] = []
        self.recall_calls: list[dict] = []

    async def recall(
        self, bank, query, *, tags, max_tokens,
        types=("world", "experience", "observation"),
        tags_match="any", budget="mid",
    ):
        self.recall_calls.append({
            "bank": bank, "query": query, "tags": tags,
            "max_tokens": max_tokens, "budget": budget,
        })
        return self._facts

    async def profile(self, bank):
        self.profile_calls.append(bank)
        return self._overlay


def _make_agent(cfg, memory, scope_registry, semantic_memory=None):
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
        semantic_memory=semantic_memory or FakeSemanticMemory(),
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
    async def test_single_recall_tagged_with_readable_tiers(self):
        """§4.3: the per-scope Honcho fan-out is retired. A fresh TEXT turn
        issues exactly ONE recall against the role's bank, tagged with the
        sensitivity tiers readable at the channel's clearance (clearance-filtered),
        not one read per active scope. (Supersedes the old test_fan_out_one_scope.)"""
        memory = Mock()
        sem = FakeSemanticMemory(facts="finance digest")

        reg = _make_scope_registry(
            readable_out=["personal", "business", "finance", "house"],
            active_out=["finance"],
            argmax_out="finance",
        )

        cfg = _make_agent_config(default_scope="personal")
        agent = _make_agent(cfg, memory, reg, semantic_memory=sem)

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "12345", "how much for the plumber?"))

        assert len(sem.recall_calls) == 1
        call = sem.recall_calls[0]
        assert call["bank"] == "casa"
        assert call["query"] == "how much for the plumber?"
        # Tags are sensitivity tiers readable at telegram clearance (private →
        # all four tiers), not the scope-registry readable set.
        assert call["tags"] == ["public", "friends", "family", "private"]

    async def test_recall_count_independent_of_active_scope_count(self):
        """§4.3: even with multiple active scopes the read issues a SINGLE
        recall (no per-scope fan-out). (Supersedes test_fan_out_two_scopes_parallel.)"""
        memory = Mock()
        sem = FakeSemanticMemory(facts="d")

        reg = _make_scope_registry(
            readable_out=["personal", "business", "finance", "house"],
            active_out=["finance", "house"],
            argmax_out="finance",
        )

        cfg = _make_agent_config(default_scope="personal")
        agent = _make_agent(cfg, memory, reg, semantic_memory=sem)

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "12345", "x"))

        assert len(sem.recall_calls) == 1
        # Tags are sensitivity tiers at telegram clearance (private → all four
        # tiers) — independent of how many active scopes the scope-registry finds.
        assert sem.recall_calls[0]["tags"] == [
            "public", "friends", "family", "private",
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

    async def test_per_turn_add_turn_is_retired(self):
        """Session-granularity save model: per-turn add_turn is retired (no
        write happens on the turn path — the reaper retains at session end)."""
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

        memory.add_turn.assert_not_called()

    async def test_memory_context_wraps_recall_without_scope_attribute(self):
        """§4.3: the recall digest is wrapped in a flat <memory_context>
        block — the legacy per-scope scope= attribute is gone (recall is a
        single tagged read, not a per-scope fan-out)."""
        memory = Mock()
        sem = FakeSemanticMemory(facts="some context data")

        reg = _make_scope_registry(
            readable_out=["personal"],
            active_out=["personal"],
            argmax_out="personal",
        )

        cfg = _make_agent_config(default_scope="personal")
        agent = _make_agent(cfg, memory, reg, semantic_memory=sem)

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "12345", "hello"))

        prompt = FakeClient.captured_options.system_prompt
        assert "<memory_context>" in prompt
        assert "scope=" not in prompt
        assert "some context data" in prompt


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
        reg.threshold = 0.35

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

        # New structured shape: message == "scope_route", fields on extra.
        routes = [r for r in caplog.records if r.message == "scope_route"]
        assert len(routes) == 1, f"expected 1 scope_route log, got {[r.message for r in routes]}"
        rec = routes[0]
        assert rec.role == "assistant"
        assert rec.channel == "telegram"
        assert set(rec.active) == {"finance", "house"}
        assert isinstance(rec.t_ms, int)


class TestScopeRouteEmission:
    """Verify agent.py emits a parser-shaped scope_route record."""

    async def test_emits_structured_fields_via_extra(self, caplog):
        import logging
        from agent import Agent
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
        reg.threshold = 0.35

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

        records = [r for r in caplog.records if r.message == "scope_route"]
        assert len(records) == 1, [r.message for r in caplog.records]
        rec = records[0]
        # Required-by-parser fields (write removed — tiering lives in the reaper):
        for key in ("channel", "role", "active", "winner",
                    "winner_score", "second_score", "threshold"):
            assert hasattr(rec, key), f"missing extra: {key}"
        assert not hasattr(rec, "write"), "write field removed from scope_route log"
        # Type sanity:
        assert isinstance(rec.winner_score, float)
        assert isinstance(rec.threshold, float)


class TestMemoryFailureLogsExcInfo:
    """E-B observability rule, carried onto the §4.3 SemanticMemory seam:
    both read-path failures ("recall failed" and "profile overlay failed")
    must carry exc_info=True so the underlying exception class + message
    reach production logs.

    Pre-fix history: the legacy per-scope read fired WARNINGs with no
    recoverable exception data (bug-review-2026-04-30-exploration2.md::E-B).
    The seam keeps the same contract on its two read calls.
    """

    async def test_recall_failure_includes_exc_info(self, caplog):
        import logging
        from agent import Agent
        from bus import BusMessage, MessageType

        memory = Mock()

        class _FailRecall(FakeSemanticMemory):
            async def recall(self, *a, **kw):
                raise TypeError(
                    "Hindsight recall got an unexpected keyword argument"
                )

        sem = _FailRecall()

        reg = _make_scope_registry(
            readable_out=["personal"],
            active_out=["personal"],
            argmax_out="personal",
        )

        cfg = _make_agent_config(default_scope="personal")
        agent = _make_agent(cfg, memory, reg, semantic_memory=sem)

        caplog.set_level(logging.WARNING, logger="agent")
        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "12345", "hello"))

        records = [
            r for r in caplog.records
            if r.name == "agent" and "recall failed" in r.getMessage()
        ]
        assert records, (
            "expected at least one 'recall failed' WARNING "
            "but got: " + repr([r.getMessage() for r in caplog.records])
        )
        rec = records[0]
        assert rec.exc_info is not None, (
            "E-B regression: recall failed warning lost exc_info=True; "
            "production root-cause is blocked"
        )
        assert rec.exc_info[0] is TypeError
        assert "keyword argument" in str(rec.exc_info[1])

    async def test_profile_overlay_failure_includes_exc_info(self, caplog):
        """E-B companion: the profile() overlay warning also carries
        exc_info — same observability rule."""
        import logging
        from agent import Agent
        from bus import BusMessage, MessageType

        memory = Mock()

        class _FailProfile(FakeSemanticMemory):
            async def profile(self, bank):
                raise RuntimeError("hindsight 503")

        sem = _FailProfile(facts="ok")

        reg = _make_scope_registry(
            readable_out=["personal"],
            active_out=["personal"],
            argmax_out="personal",
        )

        cfg = _make_agent_config(default_scope="personal")
        agent = _make_agent(cfg, memory, reg, semantic_memory=sem)

        caplog.set_level(logging.WARNING, logger="agent")
        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "12345", "hello"))

        records = [
            r for r in caplog.records
            if r.name == "agent" and "profile overlay failed" in r.getMessage()
        ]
        assert records
        rec = records[0]
        assert rec.exc_info is not None, (
            "E-B companion regression: profile overlay warning lost exc_info"
        )
        assert rec.exc_info[0] is RuntimeError
