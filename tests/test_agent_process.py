"""Integration tests for Agent._process — memory wiring + channel_context."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

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
    """Minimal ClaudeSDKClient substitute with per-attempt behaviour.

    ``failure_schedule``: list of exceptions (or None) consumed one per
    construction. A None entry means "this attempt succeeds and yields
    the usual response". Reset before each test via
    ``FakeClient.reset()``.
    """

    captured_options = None
    response_text: str = "pong"
    failure_schedule: list[Exception | None] = []
    attempts: int = 0

    @classmethod
    def reset(cls) -> None:
        cls.captured_options = None
        cls.response_text = "pong"
        cls.failure_schedule = []
        cls.attempts = 0

    def __init__(self, options):
        FakeClient.captured_options = options
        FakeClient.attempts += 1
        # The exception (if any) for this attempt is popped in query().
        if FakeClient.failure_schedule:
            self._scheduled = FakeClient.failure_schedule.pop(0)
        else:
            self._scheduled = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def query(self, text):
        self._last = text
        if self._scheduled is not None:
            raise self._scheduled

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


async def test_agent_retains_add_turn_task_strong_reference(tmp_path):
    mem = FakeMemory()
    agent = _make_agent(mem, tmp_path, role="assistant")
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    # Task completed and callback fired, set is now empty.
    # The strong reference ensures the task didn't GC while pending.
    assert len(agent._bg_tasks) == 0
    # Confirm the add_turn was persisted (proves the task ran).
    assert len(mem.add) == 1


class TestRetryIntegration:
    async def test_transient_sdk_error_retries_then_succeeds(
        self, tmp_path, caplog,
    ):
        FakeClient.reset()
        # Dynamic subclass so type name contains "CLI" → SDK_ERROR class.
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        exc = CLIConnectionError("upstream reset")
        # Fail first attempt; succeed second.
        FakeClient.failure_schedule = [exc, None]

        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")
        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()):
            text = await agent._process(_msg("telegram", "123", "hi"))

        assert text == "pong"
        assert FakeClient.attempts == 2

    async def test_one_rate_limit_then_success(self, tmp_path):
        FakeClient.reset()
        FakeClient.failure_schedule = [
            RuntimeError("429 rate limit"),
            None,
        ]
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")
        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()):
            text = await agent._process(_msg("telegram", "123", "hi"))
        assert text == "pong"
        assert FakeClient.attempts == 2

    async def test_unknown_exception_does_not_retry(self, tmp_path):
        FakeClient.reset()
        FakeClient.failure_schedule = [ValueError("bad input")]
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")
        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(ValueError):
                await agent._process(_msg("telegram", "123", "hi"))
        assert FakeClient.attempts == 1

    async def test_cancellation_propagates_without_retry(self, tmp_path):
        FakeClient.reset()
        FakeClient.failure_schedule = [asyncio.CancelledError()]
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")
        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(asyncio.CancelledError):
                await agent._process(_msg("telegram", "123", "hi"))
        assert FakeClient.attempts == 1
        sleep.assert_not_awaited()

    async def test_on_token_replays_final_text_after_retry(self, tmp_path):
        """Spec §3.2: first attempt's tokens are discarded; on_token
        sees the cumulative final text from the successful attempt."""
        FakeClient.reset()
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        exc = CLIConnectionError("upstream reset")
        FakeClient.failure_schedule = [exc, None]

        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="butler")

        seen_tokens: list[str] = []
        async def on_token(txt: str) -> None:
            seen_tokens.append(txt)

        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()):
            msg = _msg("voice", "lr", "status?")
            await agent._process(msg, on_token=on_token)

        # The final attempt emitted "pong" once via on_token. Because
        # our FakeClient raises during query() before streaming, no
        # partial tokens leaked from the failed attempt. The caller
        # sees the cumulative final text at least at the end.
        assert seen_tokens[-1] == "pong"
        assert FakeClient.attempts == 2


# ---------------------------------------------------------------------------
# Correlation-id end-to-end (spec 5.2 §7.4)
# ---------------------------------------------------------------------------


class TestCorrelationId:
    """End-to-end dispatch → every log record during the turn carries
    the cid from msg.context; two concurrent turns are distinguishable."""

    @pytest.fixture(autouse=True)
    def _cid_factory(self):
        """Install a minimal LogRecord factory that tags every new
        record with ``record.cid = cid_var.get()``. This mirrors what
        ``install_logging`` does in production but skips the
        StreamHandler (so test stdout stays clean). Needed because
        Python's ``Logger.callHandlers`` walks ancestor HANDLERS but
        NOT ancestor LOGGERS' filter chains — a filter on root would
        not fire for records from descendant loggers. The factory
        runs inside ``Logger.makeRecord``, so every record everywhere
        in the process gets tagged regardless of emission path.
        Scoped to this class via autouse; restored on teardown."""
        import logging
        from log_cid import cid_var
        orig_factory = logging.getLogRecordFactory()
        def _factory(*args, **kwargs):
            record = orig_factory(*args, **kwargs)
            record.cid = cid_var.get()
            return record
        logging.setLogRecordFactory(_factory)
        try:
            yield
        finally:
            logging.setLogRecordFactory(orig_factory)

    async def test_single_turn_records_share_cid(
        self, tmp_path, caplog,
    ):
        import logging
        from bus import BusMessage, MessageBus, MessageType

        FakeClient.reset()
        FakeClient.response_text = "pong"
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")

        bus = MessageBus()
        bus.register("assistant", agent.handle_message)

        caplog.set_level(logging.INFO)

        msg = BusMessage(
            type=MessageType.REQUEST,
            source="test", target="assistant",
            content="hi", channel="telegram",
            context={"chat_id": "42", "cid": "abcd1234"},
        )

        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()):
            loop = asyncio.create_task(bus.run_agent_loop("assistant"))
            try:
                result = await bus.request(msg, timeout=5)
            finally:
                loop.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await loop

        assert "pong" in str(result.content)

        # Every record emitted during the dispatch window must carry
        # the cid. We scope to records from Casa modules (avoid pytest's
        # own handler records).
        relevant = [
            r for r in caplog.records
            if r.name in {"agent", "retry", "bus"}
        ]
        # At least one record should have been emitted — the agent log
        # line "SDK session for 'assistant': sess-..." is unconditional
        # post-retry. Tolerate zero if the agent stays silent in this
        # fake path; the point is: none of them may have cid="-".
        cids_seen = {getattr(r, "cid", None) for r in relevant}
        # Strip any records with cid="-" that slipped in before the
        # first dispatch (there should be none, but be explicit).
        cids_seen.discard(None)
        # No stray cid="-" during the turn.
        assert cids_seen <= {"abcd1234"}, cids_seen

    async def test_two_concurrent_turns_produce_distinct_cids(
        self, tmp_path, caplog,
    ):
        import logging
        from bus import BusMessage, MessageBus, MessageType

        FakeClient.reset()
        FakeClient.response_text = "pong"
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")

        bus = MessageBus()
        bus.register("assistant", agent.handle_message)

        caplog.set_level(logging.INFO)

        def _mk(cid: str, chat_id: str) -> BusMessage:
            return BusMessage(
                type=MessageType.REQUEST,
                source="test", target="assistant",
                content="hi", channel="telegram",
                context={"chat_id": chat_id, "cid": cid},
            )

        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()):
            loop = asyncio.create_task(bus.run_agent_loop("assistant"))
            try:
                # Fire both concurrently.
                results = await asyncio.gather(
                    bus.request(_mk("aaaa1111", "A"), timeout=5),
                    bus.request(_mk("bbbb2222", "B"), timeout=5),
                )
            finally:
                loop.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await loop

        assert len(results) == 2

        # Every agent/retry/bus record must carry one of the two cids;
        # neither may cross-contaminate (dispatcher scopes cid per task).
        relevant = [
            r for r in caplog.records
            if r.name in {"agent", "retry", "bus"}
            and getattr(r, "cid", "-") != "-"
        ]
        cids_seen = {r.cid for r in relevant}
        assert cids_seen <= {"aaaa1111", "bbbb2222"}
        # Both turns emitted at least one cid-tagged record.
        assert "aaaa1111" in cids_seen
        assert "bbbb2222" in cids_seen
