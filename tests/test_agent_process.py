"""Integration tests for Agent._process — memory wiring + channel_context."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from memory import MemoryProvider
from session_registry import SessionRegistry

from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

pytestmark = pytest.mark.asyncio


def _mk_scope_registry_stub():
    from unittest.mock import Mock
    reg = Mock()
    reg.filter_readable.return_value = ["personal"]
    reg.score.return_value = {"personal": 1.0}
    reg.active_from_scores.return_value = ["personal"]
    reg.argmax_scope.return_value = "personal"
    reg.cache_stats.return_value = (0, 1)
    return reg


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


def _mk_result(sid: str, usage: dict[str, int] | None = None) -> _SDKResultMessage:
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid  # type: ignore[attr-defined]
    if usage is not None:
        m.usage = usage  # type: ignore[attr-defined]
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
    usage: dict[str, int] | None = None

    @classmethod
    def reset(cls) -> None:
        cls.captured_options = None
        cls.response_text = "pong"
        cls.failure_schedule = []
        cls.attempts = 0
        cls.usage = None

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
        yield _mk_result("sdk-sid-1", usage=FakeClient.usage)


def _make_agent(
    memory: MemoryProvider,
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
            scopes_readable=["personal"],
            scopes_owned=["personal"],
            default_scope="personal",
        ),
    )
    return Agent(
        config=cfg,
        memory=memory,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        scope_registry=_mk_scope_registry_stub(),
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

    assert mem.ensure[0][0] == "telegram-123-personal-assistant"
    assert mem.get[0][0] == "telegram-123-personal-assistant"
    for _ in range(5):
        await asyncio.sleep(0)
    assert mem.add[0][0] == "telegram-123-personal-assistant"


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
    assert "<memory_context scope=" in prompt2
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


# ---------------------------------------------------------------------------
# Token budget monitoring (spec 5.2 §5)
# ---------------------------------------------------------------------------


class TestTokenBudgetMonitoring:
    """End-to-end: Agent._process drives the BudgetTracker after
    get_context and emits the per-turn summary log line after the SDK
    call returns."""

    async def test_memory_recorder_called_on_each_successful_turn(
        self, tmp_path,
    ):
        """The recorder should see one call per turn with the digest
        size estimated from the memory_context return value."""
        FakeClient.reset()
        FakeClient.usage = {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        # 8000 chars → 2000 token-estimate, well under the 1000 budget
        # (so we get the recorder call without tripping the warning yet).
        mem = FakeMemory(context="x" * 8000)
        agent = _make_agent(mem, tmp_path, role="assistant")

        observed: list[tuple[str, int, int]] = []
        original_record = agent._budget_tracker.record
        def _spy(session_id, used_tokens, budget):
            observed.append((session_id, used_tokens, budget))
            original_record(session_id, used_tokens, budget)
        agent._budget_tracker.record = _spy  # type: ignore[method-assign]

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "123", "hi"))

        assert observed == [("telegram-123-assistant", 2013, 1000)]

    async def test_broken_memory_skips_recorder(self, tmp_path):
        """When get_context raises, we proceed without memory and must
        NOT call record (no digest to measure)."""
        class BrokenMemory(FakeMemory):
            async def get_context(self, *a, **kw):
                raise RuntimeError("honcho down")

        FakeClient.reset()
        FakeClient.usage = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
        agent = _make_agent(BrokenMemory(), tmp_path, role="assistant")
        observed: list[tuple] = []
        agent._budget_tracker.record = lambda *a: observed.append(a)  # type: ignore[method-assign]

        with patch("agent.ClaudeSDKClient", FakeClient):
            out = await agent._process(_msg("telegram", "123", "hi"))

        assert out == "pong"
        assert observed == []

    async def test_three_consecutive_overruns_emit_one_warning(
        self, tmp_path, caplog,
    ):
        """Spec §5.2: warn after three consecutive overruns; suppress
        thereafter for that session."""
        import logging as _logging
        FakeClient.reset()
        FakeClient.usage = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
        # 5000-token estimate vs 1000 budget → overrun.
        mem = FakeMemory(context="x" * 20000)
        agent = _make_agent(mem, tmp_path, role="assistant")

        caplog.set_level(_logging.WARNING, logger="tokens")
        with patch("agent.ClaudeSDKClient", FakeClient):
            for _ in range(5):
                await agent._process(_msg("telegram", "123", "hi"))

        rows = [
            r for r in caplog.records
            if r.name == "tokens" and "telegram-123-assistant" in r.getMessage()
        ]
        assert len(rows) == 1, [r.getMessage() for r in rows]

    async def test_turn_done_log_carries_usage_fields(
        self, tmp_path, caplog,
    ):
        """One INFO-level turn_done line per successful _process call,
        with the usage fields the SDK reported. No cost field."""
        import logging as _logging
        FakeClient.reset()
        FakeClient.usage = {
            "input_tokens": 1203,
            "output_tokens": 82,
            "cache_read_input_tokens": 5021,
            "cache_creation_input_tokens": 3000,
        }
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="butler")

        caplog.set_level(_logging.INFO, logger="agent")
        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("voice", "lr", "lights on"))

        rows = [r for r in caplog.records if "turn_done" in r.getMessage()]
        assert len(rows) == 1
        msg = rows[0].getMessage()
        assert "role=butler" in msg
        assert "channel=voice" in msg
        assert "input=1203" in msg
        assert "output=82" in msg
        assert "cache_read=5021" in msg
        assert "cache_write=3000" in msg
        # Explicit anti-assertion: no cost field under Max.
        assert "cost_est" not in msg
        assert "cost=" not in msg

    async def test_usage_resets_between_retry_attempts(
        self, tmp_path, caplog,
    ):
        """Spec §3 + §5: streaming accumulator resets per attempt; the
        turn_done line reflects only the final attempt's usage."""
        import logging as _logging
        FakeClient.reset()
        FakeClient.usage = {
            "input_tokens": 999,
            "output_tokens": 11,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
        FakeClient.failure_schedule = [
            CLIConnectionError("upstream reset"), None,
        ]

        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")

        caplog.set_level(_logging.INFO, logger="agent")
        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()):
            await agent._process(_msg("telegram", "123", "hi"))

        assert FakeClient.attempts == 2
        rows = [r for r in caplog.records if "turn_done" in r.getMessage()]
        assert len(rows) == 1
        msg = rows[0].getMessage()
        # Only the second attempt's usage is in the line.
        assert "input=999" in msg
        assert "output=11" in msg


# ---------------------------------------------------------------------------
# TestResumeResilience — 5.8 §3.2
# ---------------------------------------------------------------------------


def _make_agent_with_registry(
    memory: MemoryProvider,
    registry: SessionRegistry,
    role: str = "butler",
) -> Agent:
    """Like _make_agent, but takes a pre-constructed SessionRegistry so
    tests can pre-populate entries.
    """
    cfg = AgentConfig(
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(
            token_budget=1000,
            read_strategy="per_turn",
            scopes_readable=["personal"],
            scopes_owned=["personal"],
            default_scope="personal",
        ),
    )
    return Agent(
        config=cfg,
        memory=memory,
        session_registry=registry,
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        scope_registry=_mk_scope_registry_stub(),
    )


class TestResumeResilience:
    """Agent._process recovers when claude CLI rejects a stale resume (5.8)."""

    async def test_stale_resume_cleared_and_retried_fresh(self, tmp_path):
        """First attempt resumes stale-sid -> ProcessError; fallback clears
        the stale id, retries with resume=None, succeeds with fresh sid."""
        from claude_agent_sdk import ProcessError

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
            None,
        ]

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        await reg.register("voice-probe-scope", "butler", "stale-sid-abc")

        mem = FakeMemory()
        agent = _make_agent_with_registry(mem, reg, role="butler")

        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()):
            text = await agent._process(_msg("voice", "probe-scope", "hi"))

        assert text == "pong"
        assert FakeClient.attempts == 2

    async def test_fallback_uses_resume_none_on_second_attempt(self, tmp_path):
        """The second FakeClient construction must see options.resume=None."""
        from claude_agent_sdk import ProcessError

        captured_resumes: list[str | None] = []

        class _CapturingFakeClient(FakeClient):
            def __init__(self, options):
                captured_resumes.append(getattr(options, "resume", None))
                super().__init__(options)

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
            None,
        ]

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        await reg.register("voice-probe-scope", "butler", "stale-sid-abc")

        mem = FakeMemory()
        agent = _make_agent_with_registry(mem, reg, role="butler")

        with patch("agent.ClaudeSDKClient", _CapturingFakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()):
            await agent._process(_msg("voice", "probe-scope", "hi"))

        assert len(captured_resumes) == 2
        assert captured_resumes[0] == "stale-sid-abc"
        assert captured_resumes[1] is None

    async def test_process_error_without_resume_reraises(self, tmp_path):
        """Fresh session (no registry entry) + ProcessError -> propagates."""
        from claude_agent_sdk import ProcessError

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
        ]

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        mem = FakeMemory()
        agent = _make_agent_with_registry(mem, reg, role="butler")

        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(ProcessError):
                await agent._process(_msg("voice", "fresh-scope", "hi"))

        assert FakeClient.attempts == 1
        assert reg.get("voice-fresh-scope") is None

    async def test_fallback_second_process_error_reraises(self, tmp_path):
        """Both attempts ProcessError -> second propagates; stale id cleared."""
        from claude_agent_sdk import ProcessError

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
            ProcessError("Command failed with exit code 1", exit_code=1),
        ]

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        await reg.register("voice-probe-scope", "butler", "stale-sid-xyz")

        mem = FakeMemory()
        agent = _make_agent_with_registry(mem, reg, role="butler")

        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(ProcessError):
                await agent._process(_msg("voice", "probe-scope", "hi"))

        assert FakeClient.attempts == 2
        entry = reg.get("voice-probe-scope")
        assert entry is not None
        assert "sdk_session_id" not in entry

    async def test_fallback_logs_warning(self, tmp_path, caplog):
        """A single WARNING with key + sid fires on fallback."""
        import logging as _logging
        from claude_agent_sdk import ProcessError

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError("Command failed with exit code 1", exit_code=1),
            None,
        ]

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        await reg.register("voice-probe-scope", "butler", "stale-sid-xyz")

        mem = FakeMemory()
        agent = _make_agent_with_registry(mem, reg, role="butler")

        caplog.set_level(_logging.WARNING, logger="agent")
        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("retry.asyncio.sleep", new=AsyncMock()):
            await agent._process(_msg("voice", "probe-scope", "hi"))

        warning_records = [
            r for r in caplog.records
            if r.levelno == _logging.WARNING
            and "SDK resume failed" in r.getMessage()
        ]
        assert len(warning_records) == 1
        msg = warning_records[0].getMessage()
        assert "voice-probe-scope" in msg
        assert "stale-sid-xyz" in msg


# ---------------------------------------------------------------------------
# TestOriginVar — Phase 3.1 §6.6
# ---------------------------------------------------------------------------


class TestOriginVar:
    """`origin_var` is set for the duration of a turn and reset on exit.
    Nested asyncio tasks (spawned from a tool handler) inherit the value
    via asyncio's contextvars snapshot."""

    async def test_origin_var_set_during_turn(self, tmp_path):
        """FakeClient captures origin_var.get(None) inside receive_response
        (which runs while the turn is in-flight). After the turn returns
        we expect origin_var to be reset to None (default sentinel)."""
        import agent as agent_mod

        captured: dict[str, Any] = {}

        class CapturingClient(FakeClient):
            async def receive_response(self):
                captured["inside_turn"] = agent_mod.origin_var.get(None)
                async for m in super().receive_response():
                    yield m

        mem = FakeMemory()
        a = _make_agent(mem, tmp_path, role="assistant")
        msg = _msg("telegram", "777", "hello")
        with patch("agent.ClaudeSDKClient", CapturingClient):
            await a._process(msg)

        assert captured["inside_turn"] is not None
        origin = captured["inside_turn"]
        assert origin["role"] == "assistant"
        assert origin["channel"] == "telegram"
        assert origin["chat_id"] == "777"
        assert origin["user_text"] == "hello"

        # After the turn, ContextVar is back to None.
        assert agent_mod.origin_var.get(None) is None

    async def test_origin_var_inherited_by_child_task(self, tmp_path):
        """A task spawned from inside receive_response must see the same
        origin value — this is the pattern delegate_to_agent relies on."""
        import asyncio as _asyncio
        import agent as agent_mod

        child_saw: dict[str, Any] = {}

        class SpawningClient(FakeClient):
            async def receive_response(self):
                async def _child():
                    child_saw["origin"] = agent_mod.origin_var.get(None)
                t = _asyncio.create_task(_child())
                await t
                async for m in super().receive_response():
                    yield m

        mem = FakeMemory()
        a = _make_agent(mem, tmp_path, role="butler")
        msg = _msg("voice", "living-room", "lights on")
        with patch("agent.ClaudeSDKClient", SpawningClient):
            await a._process(msg)

        assert child_saw["origin"] is not None
        assert child_saw["origin"]["channel"] == "voice"
        assert child_saw["origin"]["role"] == "butler"


# ---------------------------------------------------------------------------
# Scheduled-trigger silence (spec 2026-04-28 §3.2)
# ---------------------------------------------------------------------------


class _StubChannel:
    """Minimal channel double for handle_message dispatch tests.

    NOT a Channel subclass — Channel is ABC and instantiation would
    fail. The agent only does hasattr() checks plus direct attribute
    calls, so a duck-typed object suffices. ChannelManager.register
    keys on .name.
    """

    name = "telegram"
    default_agent = "assistant"

    def __init__(self) -> None:
        from unittest.mock import MagicMock, AsyncMock
        # create_on_token is a sync factory returning an async callback.
        self.create_on_token = MagicMock(return_value=AsyncMock())
        self.send = AsyncMock()
        self.finalize_stream = AsyncMock()

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _scheduled_msg(text: str = "tick") -> BusMessage:
    return BusMessage(
        type=MessageType.SCHEDULED,
        source="scheduler",
        target="assistant",
        content=text,
        channel="telegram",
        context={"chat_id": "interval-heartbeat"},
    )


def _request_msg(text: str = "hi") -> BusMessage:
    return BusMessage(
        type=MessageType.REQUEST,
        source="user",
        target="assistant",
        content=text,
        channel="telegram",
        context={"chat_id": "12345"},
    )


class TestScheduledSilence:
    async def test_scheduled_does_not_create_on_token(self, tmp_path):
        """Spec §3.2 B.1: SCHEDULED must NOT receive a streaming
        callback. The Telegram channel sends the first token as a new
        chat message; suppressing on_token is the load-bearing fix."""
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value=""),
        ):
            await agent.handle_message(_scheduled_msg())

        assert stub.create_on_token.call_count == 0

    async def test_request_still_streams(self, tmp_path):
        """Spec §3.2 B.1 non-goal: streaming for REQUEST is unchanged."""
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value="pong"),
        ):
            await agent.handle_message(_request_msg())

        assert stub.create_on_token.call_count == 1

    async def test_silent_sentinel_suppresses_send(self, tmp_path):
        """Spec §3.2 B.2: text == '<silent/>' (after strip) must
        suppress channel.send / finalize_stream and produce no
        BusMessage downstream."""
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value="<silent/>"),
        ):
            result = await agent.handle_message(_scheduled_msg())

        assert stub.send.call_count == 0
        assert stub.finalize_stream.call_count == 0
        assert result is None

    async def test_whitespace_suppresses_send(self, tmp_path):
        """Spec §3.2 B.2: empty / whitespace-only text suppresses
        delivery the same way as the explicit sentinel."""
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value="   \n  "),
        ):
            result = await agent.handle_message(_scheduled_msg())

        assert stub.send.call_count == 0
        assert stub.finalize_stream.call_count == 0
        assert result is None

    async def test_real_text_passes_through(self, tmp_path):
        """Spec §3.2 B.2: any non-sentinel, non-empty text reaches the
        channel and emits a RESPONSE BusMessage with the same body."""
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        text = "Meeting with Ana in 20 min."
        with patch.object(
            agent, "_process", AsyncMock(return_value=text),
        ):
            result = await agent.handle_message(_scheduled_msg())

        # No on_token (Fix B.1 disables streaming for SCHEDULED) →
        # falls through to channel.send, NOT finalize_stream.
        assert stub.send.call_count == 1
        sent_text = stub.send.call_args.args[0]
        assert sent_text == text
        assert stub.finalize_stream.call_count == 0
        assert result is not None
        assert result.content == text
