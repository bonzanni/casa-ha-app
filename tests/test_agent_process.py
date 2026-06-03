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
from semantic_memory import SemanticMemory
from session_registry import SessionRegistry

from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


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
    def __init__(self, context: str = "", overlay: str = "") -> None:
        self.context = context
        self.overlay = overlay
        self.ensure: list[tuple] = []
        self.get: list[tuple] = []
        self.add: list[tuple] = []
        self.cross: list[tuple] = []
        self.overlay_calls: list[tuple] = []

    async def ensure_session(self, session_id, agent_role, user_peer="nicola"):
        self.ensure.append((session_id, agent_role, user_peer))

    async def get_context(
        self, session_id, tokens, search_query=None, agent_role=None,
    ):
        self.get.append((session_id, tokens, search_query, agent_role))
        return self.context

    async def peer_overlay_context(
        self, observer_role, user_peer, search_query, tokens,
    ):
        self.overlay_calls.append(
            (observer_role, user_peer, search_query, tokens)
        )
        return self.overlay

    async def add_turn(
        self, session_id, agent_role, user_text, assistant_text,
        user_peer="nicola",
    ):
        self.add.append(
            (session_id, agent_role, user_text, assistant_text, user_peer)
        )

    async def cross_peer_context(
        self, observer_role, query, tokens, user_peer="nicola",
    ):
        self.cross.append((observer_role, query, tokens, user_peer))
        return ""


class FakeSemanticMemory(SemanticMemory):
    """SemanticMemory double for the §4.3 read path.

    ``profile`` returns the mental-model overlay (rendered as <peer_overlay>);
    ``recall`` returns the query-specific facts digest (rendered as
    <memory_context>). Calls are recorded so tests can assert the channel-aware
    load contract (overlay at fresh-session start; recall text-only)."""

    def __init__(self, overlay: str = "", facts: str = "") -> None:
        self._overlay = overlay
        self._facts = facts
        self.profile_calls: list[str] = []
        self.recall_calls: list[dict] = []
        self.retain_calls: list[tuple] = []
        self.cross_calls: list[dict] = []

    async def retain(self, bank, items, *, async_=True):
        self.retain_calls.append((bank, items, async_))

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

    async def cross_recall(self, bank, query, *, max_tokens, budget="low"):
        self.cross_calls.append({"bank": bank, "query": query})
        return ""


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
    semantic_memory: SemanticMemory | None = None,
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


async def test_session_id_is_channel_plus_role(tmp_path):
    # §4.3: the read path no longer fans out per-scope Honcho sessions; it
    # recalls once against the role's bank. The channel+role session-key
    # contract is now asserted via the registry write_scope record.
    mem = FakeMemory()
    sem = FakeSemanticMemory(facts="recall digest")
    agent = _make_agent(mem, tmp_path, role="assistant", semantic_memory=sem)
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    # Fresh telegram session → one bank recall keyed to the shared casa bank.
    assert len(sem.recall_calls) == 1
    assert sem.recall_calls[0]["bank"] == "casa"
    # Session-granularity save model: per-turn add_turn is retired. The
    # dominant write_scope is recorded on the registry entry instead, and
    # no per-turn memory write happens.
    assert mem.add == []
    entry = agent._session_registry.get("telegram-123")
    assert entry is not None
    assert entry.get("write_scope") == "personal"


async def test_voice_channel_uses_voice_speaker_peer(tmp_path):
    # §4.3: the per-turn read no longer threads a user_peer (no ensure_session
    # / per-turn add_turn). The voice-speaker peer is carried into save_session
    # at session end instead. On the read side, a fresh voice turn pushes NO
    # overlay (blocked by clearance — voice=friends, overlay is private-only)
    # and NEVER auto-recalls (voice keeps the multi-strategy recall off the
    # first-utterance critical path). write_scope still records.
    mem = FakeMemory()
    sem = FakeSemanticMemory(overlay="OVERLAY")
    agent = _make_agent(mem, tmp_path, role="butler", semantic_memory=sem)
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("voice", "lr", "lights on"))

    assert len(sem.profile_calls) == 0   # voice clearance < private → overlay blocked
    assert sem.recall_calls == []        # voice never auto-recalls
    assert mem.add == []
    entry = agent._session_registry.get("voice-lr")
    assert entry is not None
    assert entry.get("write_scope") == "personal"


async def test_telegram_channel_autorecalls_on_fresh_session(tmp_path):
    # §4.3: the user_peer (nicola) is a save-path concern now (carried into
    # save_session), no longer threaded through the per-turn read. The
    # read-path contract for a fresh TEXT channel is: push the overlay AND
    # auto-recall the opening utterance against the role's bank.
    mem = FakeMemory()
    sem = FakeSemanticMemory(overlay="O", facts="F")
    agent = _make_agent(mem, tmp_path, role="assistant", semantic_memory=sem)
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    assert len(sem.profile_calls) == 1          # overlay pushed
    assert len(sem.recall_calls) == 1           # telegram auto-recalls
    assert sem.recall_calls[0]["query"] == "hi"


# ---------------------------------------------------------------------------
# Phase 5 / E-14: peer-overlay assembly (spec § 2.7, § 2.9)
# ---------------------------------------------------------------------------


async def test_fresh_text_turn_pushes_overlay_and_recalls(tmp_path):
    """Spec §4.3: a fresh TEXT turn pushes the mental-model overlay
    (profile) AND runs one query-specific recall over the readable scopes.
    Supersedes the old "1 overlay + N per-scope reads under gather" shape —
    the per-scope fan-out is gone; recall is a single tagged call."""
    mem = FakeMemory()
    sem = FakeSemanticMemory(overlay="overlay-content", facts="recall-content")
    agent = _make_agent(mem, tmp_path, role="assistant", semantic_memory=sem)
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    # One overlay (profile) call per fresh turn, keyed to the shared casa bank.
    assert sem.profile_calls == ["casa"]
    # One recall call: query == utterance, tags == sensitivity tiers readable at
    # telegram clearance (private → all four tiers), mid budget.
    assert len(sem.recall_calls) == 1
    call = sem.recall_calls[0]
    assert call["bank"] == "casa"
    assert call["query"] == "hi"
    assert call["tags"] == ["public", "friends", "family", "private"]
    assert call["budget"] == "mid"


async def test_overlay_block_present_when_overlay_non_empty(tmp_path):
    """Assembled memory_blocks contains <peer_overlay> when the profile
    overlay digest is non-empty (spec §4.3)."""
    mem = FakeMemory()
    sem = FakeSemanticMemory(overlay="OVERLAY_TEXT")
    agent = _make_agent(mem, tmp_path, role="assistant", semantic_memory=sem)

    captured: dict[str, str] = {}

    class _CapturingClient(FakeClient):
        def __init__(self, options):
            super().__init__(options)
            captured["system"] = options.system_prompt

    with patch("agent.ClaudeSDKClient", _CapturingClient):
        await agent._process(_msg("telegram", "123", "hi"))

    assert "<peer_overlay>" in captured["system"]
    assert "OVERLAY_TEXT" in captured["system"]


async def test_overlay_failure_does_not_poison_recall(tmp_path, caplog):
    """profile() raising → overlay omitted, but the recall still proceeds
    and lands in the prompt (spec §4.3 — the two reads are independent)."""
    import logging

    class FailingProfileMemory(FakeSemanticMemory):
        async def profile(self, bank):
            self.profile_calls.append(bank)
            raise RuntimeError("simulated failure")

    mem = FakeMemory()
    sem = FailingProfileMemory(facts="recall-still-works")
    agent = _make_agent(mem, tmp_path, role="assistant", semantic_memory=sem)

    captured: dict[str, str] = {}

    class _CapturingClient(FakeClient):
        def __init__(self, options):
            super().__init__(options)
            captured["system"] = options.system_prompt

    with caplog.at_level(logging.WARNING, logger="agent"):
        with patch("agent.ClaudeSDKClient", _CapturingClient):
            await agent._process(_msg("telegram", "123", "hi"))

    assert "<peer_overlay>" not in captured["system"]   # overlay omitted
    assert "<memory_context>" in captured["system"]     # recall present
    assert "recall-still-works" in captured["system"]
    assert any(
        "profile overlay failed" in r.message for r in caplog.records
    )


# NOTE (§4.3 read-path rewire): test_peer_overlay_empty_logs_info_line was
# dropped here. It asserted the legacy `peer_overlay_empty` INFO line emitted
# by the old MemoryProvider.peer_overlay_context read path. The SemanticMemory
# seam's `profile()` overlay has no empty-digest observability line (it would
# need to be designed + added deliberately, not inferred), so this assertion
# has no equivalent on the new contract. No replacement is invented.


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
    # §4.3: <memory_context> now wraps the single recall digest (no per-scope
    # scope= attribute). Empty recall → no block; non-empty → block + content.
    mem = FakeMemory()
    sem = FakeSemanticMemory(facts="")
    agent = _make_agent(mem, tmp_path, role="assistant", semantic_memory=sem)
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))
    assert "<memory_context>" not in FakeClient.captured_options.system_prompt

    # Force a FRESH session so recall fires (the first turn above persisted a
    # registry entry into the shared sessions.json, which would otherwise
    # resume and skip the recall under §4.3).
    sem2 = FakeSemanticMemory(facts="## Recent\n[nicola] hi")
    agent2 = _make_agent(mem, tmp_path, role="assistant", semantic_memory=sem2)
    with patch("agent.ClaudeSDKClient", FakeClient), \
         patch("agent._resume_decision", return_value=("new", False)):
        await agent2._process(_msg("telegram", "123", "hi"))
    prompt2 = FakeClient.captured_options.system_prompt
    assert "<memory_context>" in prompt2
    assert "scope=" not in prompt2          # per-scope attribute is gone
    assert "[nicola] hi" in prompt2


async def test_write_scope_recorded_on_registry_no_per_turn_write(tmp_path):
    """Session-granularity save model (spec §4.2): the agent no longer does a
    per-turn memory write; it records the turn's dominant write_scope on the
    registry entry, and the actual retain happens at session end."""
    mem = FakeMemory()
    agent = _make_agent(mem, tmp_path, role="assistant")
    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(_msg("telegram", "123", "hi"))

    assert FakeClient.response_text == "pong"
    # No per-turn add_turn write happens any more.
    assert mem.add == []
    entry = agent._session_registry.get("telegram-123")
    assert entry is not None
    assert entry.get("write_scope") == "personal"


async def test_memory_failure_does_not_break_response(tmp_path, caplog):
    import logging

    class BrokenSemanticMemory(FakeSemanticMemory):
        async def recall(self, *a, **kw):
            raise RuntimeError("hindsight down")

    mem = FakeMemory()
    sem = BrokenSemanticMemory()
    agent = _make_agent(mem, tmp_path, role="assistant", semantic_memory=sem)
    with patch("agent.ClaudeSDKClient", FakeClient):
        with caplog.at_level(logging.WARNING):
            out = await agent._process(_msg("telegram", "123", "hi"))
    assert out == "pong"
    assert any("recall failed" in r.message.lower() for r in caplog.records)
    prompt = FakeClient.captured_options.system_prompt
    assert "<memory_context>" not in prompt
    assert "<channel_context>" in prompt


# NOTE (Task 7, spec §4.2): the per-turn `add_turn` write and its background
# task wrapper (`_add_turn_bg`) were retired in favour of session-granularity
# saves. Two tests that exercised that removed machinery were dropped here:
#   - test_add_turn_failure_logs_warning: asserted the now-deleted
#     `_add_turn_bg` try/except logged a warning when add_turn raised. The
#     agent no longer calls add_turn per turn, so the path is gone.
#   - test_agent_retains_add_turn_task_strong_reference: asserted the
#     background add_turn task was strongly referenced in `_bg_tasks`. The
#     write is now an inline `record_write_scope` await (no spawned task).
# Per-turn write coverage is replaced by the registry-write_scope assertions
# in test_write_scope_recorded_on_registry_no_per_turn_write (this file) and
# the scope-routing assertions in test_agent_process_scope.py.


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


class TestAssistantMessageSeparator:
    """E-2: Ellen's cumulative attempt_text must insert \\n\\n between
    successive AssistantMessages, so the streamed message reads as discrete
    thoughts rather than one glued paragraph
    (bug-review-2026-04-29-exploration.md § E-2)."""

    async def test_two_assistant_messages_get_double_newline_separator(
        self, tmp_path,
    ):
        """Feed SDK with ack + final answer as two AssistantMessages; the
        last on_token argument must contain '\\n\\n' between them."""

        class _TwoMsgClient(FakeClient):
            async def receive_response(self):
                if self._scheduled is not None:
                    raise self._scheduled
                yield _mk_assistant("Let me ask Tina to pull the house state for you.")
                yield _mk_assistant("Here's the snapshot: lights are off.")
                yield _mk_result("sdk-sid-e2", usage=FakeClient.usage)

        FakeClient.reset()
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")

        seen: list[str] = []

        async def on_token(txt: str) -> None:
            seen.append(txt)

        with patch("agent.ClaudeSDKClient", _TwoMsgClient):
            msg = _msg("telegram", "123", "are the lights on?")
            await agent._process(msg, on_token=on_token)

        # on_token was called at least twice (once per AssistantMessage)
        assert len(seen) >= 2, f"expected ≥2 on_token calls, got {len(seen)}: {seen}"
        last = seen[-1]
        # Both pieces present in the cumulative final
        assert "Let me ask Tina" in last
        assert "Here's the snapshot" in last
        # The bug: pre-fix the cumulative reads "...for you.Here's..."
        assert "for you.Here's" not in last, (
            f"E-2 not fixed: ack and answer concatenated without separator. "
            f"Final cumulative on_token text: {last!r}"
        )
        # Positive: \n\n between the two AssistantMessages
        assert "for you.\n\nHere's the snapshot" in last, (
            f"Expected '\\n\\n' between successive AssistantMessages. Got: {last!r}"
        )


class TestPhase4bDispatch:
    """Phase 4b Bug 3 parity: Ellen's _attempt_sdk_turn dispatches
    every SDK message kind through sdk_logging, identical shape to
    the in_casa-driver path."""

    async def test_attempt_sdk_turn_logs_per_message(
        self, tmp_path, caplog,
    ):
        import logging
        from claude_agent_sdk import (
            AssistantMessage as _AM, ResultMessage as _RM,
            SystemMessage as _SM, TextBlock as _TB,
        )

        class _RichFakeClient:
            captured_options = None
            def __init__(self, options):
                _RichFakeClient.captured_options = options

            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, text): pass

            async def receive_response(self):
                init = _SM.__new__(_SM)
                init.subtype = "init"  # type: ignore[attr-defined]
                init.data = {"model": "sonnet", "session_id": "abcdef12-fff"}  # type: ignore[attr-defined]
                yield init
                yield _mk_assistant("Hi.")
                rm = _RM.__new__(_RM)
                rm.session_id = "abcdef12-fff"  # type: ignore[attr-defined]
                rm.usage = {"input_tokens": 50, "output_tokens": 5}  # type: ignore[attr-defined]
                rm.num_turns = 1  # type: ignore[attr-defined]
                rm.total_cost_usd = 0.0001  # type: ignore[attr-defined]
                yield rm

        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")

        with caplog.at_level(logging.DEBUG, logger="sdk"):
            with patch("agent.ClaudeSDKClient", _RichFakeClient):
                await agent._process(_msg("telegram", "200", "hi"))

        msgs = [r.getMessage() for r in caplog.records if r.name == "sdk"]
        assert any("system_init" in m and "model=sonnet" in m for m in msgs), msgs
        assert any("assistant_message idx=1" in m and "chars=3" in m for m in msgs), msgs
        assert any("turn_done" in m and "turns=1" in m for m in msgs), msgs

    async def test_attempt_sdk_turn_injects_stderr_callback(
        self, tmp_path,
    ):
        """Bug 4: ClaudeAgentOptions passed to ClaudeSDKClient must
        carry our stderr callback so the SDK pipes the CLI subprocess
        stderr through subprocess_cli.connect()."""
        FakeClient.reset()
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "201", "hi"))

        opts = FakeClient.captured_options
        assert opts is not None, "FakeClient never received options"
        assert callable(opts.stderr), (
            "Phase 4b Bug 4: ClaudeAgentOptions.stderr must be set "
            "to make_stderr_logger's callback before ClaudeSDKClient is built"
        )


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
        size estimated from the assembled memory_blocks (spec §4.3:
        the recall facts wrapped in <memory_context>)."""
        from tokens import estimate_tokens
        FakeClient.reset()
        FakeClient.usage = {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        facts = "x" * 8000
        # Overlay empty so the recorded block is exactly the recall context.
        sem = FakeSemanticMemory(overlay="", facts=facts)
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant", semantic_memory=sem)

        observed: list[tuple[str, int, int]] = []
        original_record = agent._budget_tracker.record
        def _spy(session_id, used_tokens, budget):
            observed.append((session_id, used_tokens, budget))
            original_record(session_id, used_tokens, budget)
        agent._budget_tracker.record = _spy  # type: ignore[method-assign]

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "123", "hi"))

        expected = estimate_tokens(
            f"<memory_context>\n{facts}\n</memory_context>"
        )
        assert observed == [("telegram-123-assistant", expected, 1000)]

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
        # §4.3: recall (and thus the oversized memory_blocks) only fires on a
        # FRESH session, so force every turn fresh — the budget streak is keyed
        # on channel_key+role, which is stable across these five fresh turns.
        mem = FakeMemory()
        sem = FakeSemanticMemory(facts="x" * 20000)
        agent = _make_agent(mem, tmp_path, role="assistant", semantic_memory=sem)

        caplog.set_level(_logging.WARNING, logger="tokens")
        with patch("agent.ClaudeSDKClient", FakeClient), \
             patch("agent._resume_decision", return_value=("new", False)):
            for _ in range(5):
                await agent._process(_msg("telegram", "123", "hi"))

        rows = [
            r for r in caplog.records
            if r.name == "tokens" and "telegram-123-assistant" in r.getMessage()
        ]
        assert len(rows) == 1, [r.getMessage() for r in rows]

    async def test_turn_tokens_log_carries_usage_fields(
        self, tmp_path, caplog,
    ):
        """One INFO-level turn_tokens line per successful _process call,
        with the usage fields the SDK reported. No cost field.
        (G-fix 2026-05-29: the agent-logger summary prefix is now
        ``turn_tokens``, disjoint from the sdk logger's ``turn_done``.)"""
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

        # Phase 4b added an `sdk` logger turn_done line; this test asserts
        # the `agent` logger's per-turn summary specifically (now `turn_tokens`).
        rows = [
            r for r in caplog.records
            if r.name == "agent" and "turn_tokens" in r.getMessage()
        ]
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
        # Phase 4b added an `sdk` logger turn_done line; the `agent` logger
        # summary still fires once per overall _process call (now `turn_tokens`).
        rows = [
            r for r in caplog.records
            if r.name == "agent" and "turn_tokens" in r.getMessage()
        ]
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
        semantic_memory=FakeSemanticMemory(),
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

    async def test_sdk_retry_fresh_emits_telemetry(self, tmp_path, caplog):
        """Bug 5: when the resume → ProcessError → fresh-retry path fires,
        one structured INFO line records the event with exit_code,
        prior_sid, and stderr_tail. Verifiable via caplog."""
        import logging
        from claude_agent_sdk import ProcessError

        FakeClient.reset()
        FakeClient.failure_schedule = [
            ProcessError(
                "CLI exit",
                exit_code=1,
                stderr="stale-session-error\nat line 42",
            ),
            None,
        ]
        FakeClient.response_text = "ok after retry"

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        await reg.register("telegram-202", "butler", "STALE-SID-1")

        mem = FakeMemory()
        agent = _make_agent_with_registry(mem, reg, role="butler")

        with caplog.at_level(logging.INFO, logger="agent"):
            with patch("agent.ClaudeSDKClient", FakeClient), \
                 patch("retry.asyncio.sleep", new=AsyncMock()):
                await agent._process(_msg("telegram", "202", "hi"))

        msgs = [r.getMessage() for r in caplog.records if r.name == "agent"]
        retry_lines = [m for m in msgs if "sdk_retry_fresh" in m]
        assert len(retry_lines) == 1, (
            f"expected exactly one sdk_retry_fresh INFO line; got: {retry_lines}"
        )
        line = retry_lines[0]
        assert "exit_code=1" in line, line
        assert "prior_sid=STALE-SID-1" in line, line
        # stderr_tail truncated at 200 chars; newlines escaped to \\n
        assert "stderr_tail=" in line, line
        assert "stale-session-error" in line, line
        # \n in stderr → \\n in tail
        assert "\\n" in line, line

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

    async def test_silent_sentinel_suppresses_send_on_request_turn(
        self, tmp_path,
    ):
        """G-3 (v0.33.0, exploration2): on a USER-driven REQUEST turn,
        a bare `<silent/>` accumulated text must also suppress the send.

        Pre-fix the suppression was scoped to MessageType.SCHEDULED, so
        Ellen's outer turn after a configurator engagement (cid
        `dcc3c30b` 2026-05-01) leaked the literal sentinel into a user
        DM. Lifting the SCHEDULED-only gate generalizes the noop
        contract."""
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value="<silent/>"),
        ):
            result = await agent.handle_message(_request_msg())

        assert stub.send.call_count == 0
        assert stub.finalize_stream.call_count == 0
        assert result is None

    async def test_whitespace_suppresses_send_on_request_turn(self, tmp_path):
        """G-3 (v0.33.0): whitespace-only accumulated text on a
        USER-driven REQUEST turn must also suppress delivery, matching
        the SCHEDULED behavior."""
        mem = FakeMemory()
        agent = _make_agent(mem, tmp_path, role="assistant")
        stub = _StubChannel()
        agent._channel_manager.register(stub)

        with patch.object(
            agent, "_process", AsyncMock(return_value="   \n  "),
        ):
            result = await agent.handle_message(_request_msg())

        assert stub.send.call_count == 0
        assert stub.finalize_stream.call_count == 0
        assert result is None


# ---------------------------------------------------------------------------
# TestSaveBeforeOverwrite — spec §4.2 C1, Task 7
# ---------------------------------------------------------------------------


class TestSaveBeforeOverwrite:
    """_process must await save_session exactly once, before opening a new
    SDK session, when a cold prior session exists (spec §4.2 save-before-
    overwrite branch).  The pure helper _resume_decision is already unit-
    tested in test_agent_save_path.py; this class covers the load-bearing
    call inside _process itself."""

    async def test_save_session_called_on_cold_path(self, tmp_path, monkeypatch):
        """When the registry holds a stale telegram entry (idle > 12 h),
        _resume_decision returns ("new", True) and _process must await
        save_session once with the correct positional / keyword args."""
        import agent as agent_mod
        from datetime import datetime, timedelta, timezone

        save_calls: list[dict] = []

        async def _fake_save(channel_key, session_registry, semantic_memory,
                             *, role, directory, user_peer):
            save_calls.append({
                "channel_key": channel_key,
                "role": role,
                "directory": directory,
                "user_peer": user_peer,
            })

        monkeypatch.setattr(agent_mod, "save_session", _fake_save)
        FakeClient.reset()

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        # Inject a stale entry: sdk_session_id present, last_active > 12 h ago.
        stale_ts = (
            datetime.now(timezone.utc) - timedelta(hours=13)
        ).isoformat()
        reg._data["telegram-123"] = {
            "agent": "assistant",
            "sdk_session_id": "old-stale-sid",
            "last_active": stale_ts,
        }

        mem = FakeMemory()
        agent = _make_agent_with_registry(mem, reg, role="assistant")

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "123", "hi"))

        assert len(save_calls) == 1, (
            f"expected save_session called once on cold path; got {len(save_calls)} calls"
        )
        call = save_calls[0]
        assert call["channel_key"] == "telegram-123", call
        assert call["role"] == "assistant", call
        assert call["directory"].endswith("agent-home/assistant"), call
        assert call["user_peer"] == "nicola", call

    async def test_save_session_not_called_on_resume_path(self, tmp_path, monkeypatch):
        """When the registry holds a fresh entry (idle < 12 h), _resume_decision
        returns ("resume", False) and _process must NOT call save_session."""
        import agent as agent_mod
        from datetime import datetime, timedelta, timezone

        save_calls: list[dict] = []

        async def _fake_save(channel_key, session_registry, semantic_memory,
                             *, role, directory, user_peer):
            save_calls.append({"channel_key": channel_key})

        monkeypatch.setattr(agent_mod, "save_session", _fake_save)
        FakeClient.reset()

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        # Fresh entry: last_active just 5 minutes ago → resume, no save.
        fresh_ts = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        reg._data["telegram-456"] = {
            "agent": "assistant",
            "sdk_session_id": "fresh-live-sid",
            "last_active": fresh_ts,
        }

        mem = FakeMemory()
        agent = _make_agent_with_registry(mem, reg, role="assistant")

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "456", "hi"))

        assert save_calls == [], (
            f"save_session must not be called on the resume path; got {save_calls}"
        )

    async def test_resumed_session_skips_overlay_and_recall(
        self, tmp_path, monkeypatch,
    ):
        """Spec §4.3: on a resumed session (is_fresh=False), _plan_load
        returns push_overlay=False, auto_recall=False. The overlay and
        per-turn recall are BOTH skipped — the overlay rides along on the
        resumed SDK thread; no auto-recall fires."""
        import agent as agent_mod
        from datetime import datetime, timedelta, timezone

        # Monkeypatch save_session so the save-before-overwrite path can't
        # accidentally fire and muddy the assertion.
        monkeypatch.setattr(agent_mod, "save_session", AsyncMock())
        FakeClient.reset()

        reg = SessionRegistry(str(tmp_path / "sessions.json"))
        # Seed a FRESH entry: sdk_session_id present, last_active just 5 min ago
        # → _resume_decision returns ("resume", False) → is_fresh=False.
        fresh_ts = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        reg._data["telegram-789"] = {
            "agent": "assistant",
            "sdk_session_id": "live-sid-resume",
            "last_active": fresh_ts,
        }

        fake = FakeSemanticMemory(overlay="should-not-appear", facts="should-not-appear")
        mem = FakeMemory()
        agent = _make_agent_with_registry(mem, reg, role="assistant")
        # Replace the NoOp semantic memory with our tracking fake.
        agent._semantic_memory = fake

        with patch("agent.ClaudeSDKClient", FakeClient):
            await agent._process(_msg("telegram", "789", "hi"))

        assert fake.profile_calls == [], (
            "profile() must not be called on a resumed session (overlay rides "
            f"along on the thread); got: {fake.profile_calls}"
        )
        assert fake.recall_calls == [], (
            "recall() must not be called on a resumed session (no auto-recall "
            f"on resume path); got: {fake.recall_calls}"
        )
