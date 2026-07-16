"""Voice turn budget, sync-only modes, and deterministic progress (spec A4).

Covers:
- TestVoiceModes: async/interactive delegation is rejected outright on the
  voice channel (never coerced to sync, never `pending`).
- TestVoiceDeadline: an expired/missing voice deadline degrades to a typed
  `deadline_exceeded` FAST (never the 60s general sync ceiling).
- TestVoiceTurnBudget: channels/voice/channel.py's `_voice_turn_budget_s()`
  — default 27s, hard-capped at 27s regardless of configuration.
- TestVoiceProgressSink: the deterministic "still working" block is written
  exactly once per outer voice turn and suppressed once the turn has
  spoken real content.
- TestVoiceDeadlineOriginPropagation: agent.py's `_process` propagates the
  voice deadline + progress sink into origin ONLY for the voice channel.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bus import BusMessage, MessageBus, MessageType
from casa_core_middleware import cid_middleware
from channels.voice.channel import VoiceChannel
from config import AgentConfig, DelegateEntry

pytestmark = pytest.mark.unit


def _cfg(role: str, delegates: tuple[str, ...] = ()) -> AgentConfig:
    cfg = AgentConfig(role=role)
    cfg.delegates = [DelegateEntry(agent=d, purpose="p", when="w") for d in delegates]
    return cfg


def _voice_origin(**overrides) -> dict:
    origin = {
        "role": "assistant", "execution_role": "assistant",
        "channel": "voice", "chat_id": "c1", "cid": "t", "user_text": "hi",
    }
    origin.update(overrides)
    return origin


def _voice_output(tm, text: str):
    return tm.DelegatedOutput(text=text, structured_output={
        "status": "answered", "spoken_summary": text, "answer": text,
        "clarification": "", "citations": [], "assumptions": [],
        "provenance": {}, "sensitivity": "household",
        "delivery_ttl_s": 900,
    })


def _init_tools_for_voice():
    import tools as tm

    reg = MagicMock()
    reg.get.return_value = None
    reg.register_delegation = AsyncMock()
    reg.cancel_delegation = AsyncMock()
    reg.fail_delegation = AsyncMock()
    reg.complete_delegation = AsyncMock()
    reg.job_registry.finish_voice_result = AsyncMock()
    tm.init_tools(
        channel_manager=MagicMock(), bus=MagicMock(),
        specialist_registry=reg, mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_role_map={
            "assistant": _cfg("assistant", delegates=("finance",)),
            "finance": _cfg("finance"),
        },
    )
    return tm, reg


# ---------------------------------------------------------------------------
# TestVoiceModes — async/interactive rejected outright on voice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestVoiceModes:
    async def test_async_mode_rejected_on_voice(self):
        import agent as agent_mod

        tm, reg = _init_tools_for_voice()
        token = agent_mod.origin_var.set(_voice_origin(
            voice_deadline=asyncio.get_running_loop().time() + 20.0,
        ))
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "async",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "mode_unsupported_on_voice"
        # No side effect happened — the specialist never launched.
        reg.register_delegation.assert_not_awaited()

    async def test_interactive_mode_rejected_on_voice(self):
        import agent as agent_mod

        tm, reg = _init_tools_for_voice()
        token = agent_mod.origin_var.set(_voice_origin(
            voice_deadline=asyncio.get_running_loop().time() + 20.0,
        ))
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "",
                "mode": "interactive",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "mode_unsupported_on_voice"
        reg.register_delegation.assert_not_awaited()

    async def test_sync_mode_still_allowed_on_voice(self, monkeypatch):
        """Control: sync is the ONE mode voice permits."""
        import agent as agent_mod

        tm, reg = _init_tools_for_voice()

        async def _fake_run(
            cfg, task_text, context_text, resolution=None, output_format=None,
        ):
            assert output_format is tm.VOICE_JOB_OUTPUT_FORMAT
            return _voice_output(tm, "ok")

        monkeypatch.setattr(tm, "_run_delegated_agent", _fake_run)

        token = agent_mod.origin_var.set(_voice_origin(
            voice_deadline=asyncio.get_running_loop().time() + 20.0,
        ))
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "finance", "task": "t", "context": "", "mode": "sync",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "ok"

    async def test_voice_interactive_to_resident_hits_mode_gate_first(self):
        """IMPORTANT 1 (review): the voice mode gate must precede the
        resident-interactive-compat check. A voice interactive delegation
        to a DECLARED RESIDENT target must return mode_unsupported_on_voice
        — NOT interactive_not_supported (which would be an observable
        ordering bypass of the voice mode gate)."""
        import agent as agent_mod
        import tools as tm

        reg = MagicMock()
        reg.get.return_value = None
        reg.register_delegation = AsyncMock()
        reg.cancel_delegation = AsyncMock()
        # A resident target: channels non-empty. Declared by assistant.
        butler = _cfg("butler")
        butler.channels = ["voice"]
        tm.init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=reg, mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=MagicMock(),
            agent_role_map={
                "assistant": _cfg("assistant", delegates=("butler",)),
                "butler": butler,
            },
        )
        token = agent_mod.origin_var.set(_voice_origin(
            voice_deadline=asyncio.get_running_loop().time() + 20.0,
        ))
        try:
            res = await tm.delegate_to_agent.handler({
                "agent": "butler", "task": "t", "context": "",
                "mode": "interactive",
            })
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "mode_unsupported_on_voice", payload


# ---------------------------------------------------------------------------
# TestVoiceDeadline — expired/missing deadline -> deadline_exceeded, FAST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestVoiceDeadline:
    async def test_expired_deadline_returns_deadline_exceeded_without_launch(
        self, monkeypatch,
    ):
        """An already-expired deadline short-circuits BEFORE the specialist
        task is created — no register_delegation, no _run_delegated_agent
        call at all."""
        import agent as agent_mod

        tm, reg = _init_tools_for_voice()

        launched = False

        async def _run(
            cfg, task_text, context_text, resolution=None, output_format=None,
        ):
            nonlocal launched
            launched = True
            return _voice_output(tm, "should never run")

        monkeypatch.setattr(tm, "_run_delegated_agent", _run)

        token = agent_mod.origin_var.set(_voice_origin(
            voice_deadline=asyncio.get_running_loop().time() - 1.0,  # expired
        ))
        try:
            res = await asyncio.wait_for(
                tm.delegate_to_agent.handler({
                    "agent": "finance", "task": "t", "context": "",
                    "mode": "sync",
                }),
                timeout=5.0,
            )
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "deadline_exceeded"
        # Never launched — pre-task short-circuit.
        assert launched is False
        reg.register_delegation.assert_not_awaited()

    async def test_immediate_task_expired_deadline_returns_deadline_not_ok(
        self, monkeypatch,
    ):
        """IMPORTANT 2 (review) regression: an immediately-returning task +
        an already-expired deadline must return deadline_exceeded, NOT ok.
        (The old `asyncio.wait(timeout=0)` gave a fast task one scheduler
        tick and could return ok past an expired deadline.)"""
        import agent as agent_mod

        tm, reg = _init_tools_for_voice()

        async def _instant(
            cfg, task_text, context_text, resolution=None, output_format=None,
        ):
            return _voice_output(tm, "done instantly")

        monkeypatch.setattr(tm, "_run_delegated_agent", _instant)

        token = agent_mod.origin_var.set(_voice_origin(
            voice_deadline=asyncio.get_running_loop().time() - 0.001,  # just expired
        ))
        try:
            res = await asyncio.wait_for(
                tm.delegate_to_agent.handler({
                    "agent": "finance", "task": "t", "context": "",
                    "mode": "sync",
                }),
                timeout=5.0,
            )
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "deadline_exceeded", payload
        assert payload["status"] == "error"

    async def test_missing_deadline_treated_as_expired(self, monkeypatch):
        """A voice origin with no voice_deadline (should not happen in
        prod) fails fast rather than risking an unbounded wait."""
        import agent as agent_mod

        tm, reg = _init_tools_for_voice()

        launched = False

        async def _run(
            cfg, task_text, context_text, resolution=None, output_format=None,
        ):
            nonlocal launched
            launched = True
            return _voice_output(tm, "should never run")

        monkeypatch.setattr(tm, "_run_delegated_agent", _run)

        token = agent_mod.origin_var.set(_voice_origin())  # no voice_deadline
        try:
            res = await asyncio.wait_for(
                tm.delegate_to_agent.handler({
                    "agent": "finance", "task": "t", "context": "",
                    "mode": "sync",
                }),
                timeout=5.0,
            )
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "deadline_exceeded"
        assert launched is False

    async def test_deadline_expires_during_wait_tears_down(self, monkeypatch):
        """A deadline that IS valid at launch but the specialist runs past
        the budget → post-launch _voice_deadline_exceeded fires: task
        cancelled, cancel_delegation awaited, typed deadline_exceeded."""
        import agent as agent_mod

        tm, reg = _init_tools_for_voice()

        async def _slow(
            cfg, task_text, context_text, resolution=None, output_format=None,
        ):
            await asyncio.sleep(10)  # cooperatively cancellable
            return _voice_output(tm, "too late")

        monkeypatch.setattr(tm, "_run_delegated_agent", _slow)

        # voice_wait_s = min(remaining - reserve, 60) ≈ 0.05s of real budget.
        deadline = (asyncio.get_running_loop().time()
                    + tm._VOICE_FALLBACK_RESERVE_S + 0.05)
        token = agent_mod.origin_var.set(_voice_origin(voice_deadline=deadline))
        try:
            res = await asyncio.wait_for(
                tm.delegate_to_agent.handler({
                    "agent": "finance", "task": "t", "context": "",
                    "mode": "sync",
                }),
                timeout=5.0,
            )
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "deadline_exceeded"
        # This path DID launch, so teardown cancels the registered delegation.
        reg.register_delegation.assert_awaited()
        reg.cancel_delegation.assert_awaited()

    async def test_registration_consuming_budget_bails_without_launch(
        self, monkeypatch,
    ):
        """IMPORTANT (re-review) 1: register_delegation's own wall-clock cost
        must be counted. A deadline that has budget AT ENTRY but is exhausted
        BY register_delegation must (a) return deadline_exceeded, (b) never
        launch the specialist, and (c) cancel the just-registered record (no
        orphan tombstone). Proves the post-register recompute, not the stale
        pre-register wait, decides the launch."""
        import agent as agent_mod

        tm, reg = _init_tools_for_voice()

        launched = False

        async def _run(
            cfg, task_text, context_text, resolution=None, output_format=None,
        ):
            nonlocal launched
            launched = True
            return _voice_output(tm, "should never run")

        monkeypatch.setattr(tm, "_run_delegated_agent", _run)

        # register_delegation deliberately OUTLIVES the ~0.2s entry budget.
        async def _slow_register(record):
            await asyncio.sleep(0.5)

        reg.register_delegation = AsyncMock(side_effect=_slow_register)

        # Budget at entry ≈ 0.2s (> 0, so the pre-register check passes),
        # but registration burns 0.5s → post-register recompute is < 0.
        deadline = (asyncio.get_running_loop().time()
                    + tm._VOICE_FALLBACK_RESERVE_S + 0.2)
        token = agent_mod.origin_var.set(_voice_origin(voice_deadline=deadline))
        try:
            res = await asyncio.wait_for(
                tm.delegate_to_agent.handler({
                    "agent": "finance", "task": "t", "context": "",
                    "mode": "sync",
                }),
                timeout=5.0,
            )
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "deadline_exceeded", payload
        assert launched is False                       # never launched
        reg.register_delegation.assert_awaited()       # it WAS registered
        reg.cancel_delegation.assert_awaited()         # then cancelled (no orphan)

    async def test_nan_deadline_fails_closed_fast(self, monkeypatch):
        """IMPORTANT (re-review) 2: a NaN propagated deadline must fail closed
        as deadline_exceeded — NOT disable the timeout. asyncio.wait(timeout=
        nan) never expires, so this test MUST complete fast (wait_for 5s
        backstop) and the specialist must never launch."""
        import agent as agent_mod

        tm, reg = _init_tools_for_voice()

        launched = False

        async def _run(
            cfg, task_text, context_text, resolution=None, output_format=None,
        ):
            nonlocal launched
            launched = True
            await asyncio.sleep(10)
            return _voice_output(tm, "nope")

        monkeypatch.setattr(tm, "_run_delegated_agent", _run)

        token = agent_mod.origin_var.set(_voice_origin(
            voice_deadline=float("nan"),
        ))
        try:
            res = await asyncio.wait_for(
                tm.delegate_to_agent.handler({
                    "agent": "finance", "task": "t", "context": "",
                    "mode": "sync",
                }),
                timeout=5.0,
            )
        finally:
            agent_mod.origin_var.reset(token)

        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "deadline_exceeded", payload
        assert launched is False
        # Never even registered — the NaN is caught at the pre-register check.
        reg.register_delegation.assert_not_awaited()


# ---------------------------------------------------------------------------
# TestVoiceTeardown — IMPORTANT 5: post-cancellation exception retrieval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestVoiceTeardown:
    async def test_task_raising_within_teardown_bound_is_retrieved(self):
        """A specialist that catches CancelledError and raises WITHIN the
        teardown bound lands in `done` (not `pending`); its exception must
        still be retrieved (never 'exception was never retrieved')."""
        import tools as tm

        _, reg = _init_tools_for_voice()

        async def _catch_then_raise():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise RuntimeError("boom during teardown")

        task = asyncio.create_task(_catch_then_raise())
        await asyncio.sleep(0.01)  # let it reach the sleep

        res = await tm._voice_deadline_exceeded(task, "did1234", "finance")
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "deadline_exceeded"
        assert task.done()
        # Retrieving here must not raise 'never retrieved' bookkeeping — the
        # callback already retrieved it; RuntimeError is observable.
        assert isinstance(task.exception(), RuntimeError)

    async def test_survivor_task_exception_retrieved_via_callback(
        self, monkeypatch,
    ):
        """A specialist that survives PAST the teardown bound lands in
        `pending`; the unconditionally-attached callback must still
        retrieve its eventual exception."""
        import tools as tm

        _, reg = _init_tools_for_voice()
        monkeypatch.setattr(tm, "_VOICE_TEARDOWN_BOUND_S", 0.02)

        started = asyncio.Event()

        async def _stubborn():
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # ignore the cancel, keep running past the teardown bound,
                # then finish with an exception
                await asyncio.sleep(0.08)
                raise RuntimeError("late boom")

        task = asyncio.create_task(_stubborn())
        await started.wait()

        res = await tm._voice_deadline_exceeded(task, "did5678", "finance")
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "deadline_exceeded"
        # Survived the 0.02s bound — still running when the helper returned.
        assert not task.done()
        # The callback retrieves its eventual exception (no leak).
        await asyncio.sleep(0.2)
        assert task.done()
        assert isinstance(task.exception(), RuntimeError)


# ---------------------------------------------------------------------------
# TestVoiceTurnBudget — channels/voice/channel.py's _voice_turn_budget_s()
# ---------------------------------------------------------------------------


class TestVoiceTurnBudget:
    def test_default_is_27(self, monkeypatch):
        monkeypatch.delenv("VOICE_TURN_BUDGET_SECONDS", raising=False)
        from channels.voice.channel import _voice_turn_budget_s
        assert _voice_turn_budget_s() == 27.0

    def test_hard_capped_at_27_even_if_configured_higher(self, monkeypatch):
        monkeypatch.setenv("VOICE_TURN_BUDGET_SECONDS", "100")
        from channels.voice.channel import _voice_turn_budget_s
        assert _voice_turn_budget_s() == 27.0

    def test_respects_a_lower_configured_value(self, monkeypatch):
        monkeypatch.setenv("VOICE_TURN_BUDGET_SECONDS", "10")
        from channels.voice.channel import _voice_turn_budget_s
        assert _voice_turn_budget_s() == 10.0

    def test_nan_config_clamped_to_default(self, monkeypatch):
        """IMPORTANT (re-review) 2: a non-finite VOICE_TURN_BUDGET_SECONDS
        (nan/inf) must be rejected and fall back to 27 — a NaN budget would
        propagate through min() and defeat the deadline (hang)."""
        from channels.voice.channel import _voice_turn_budget_s
        monkeypatch.setenv("VOICE_TURN_BUDGET_SECONDS", "nan")
        assert _voice_turn_budget_s() == 27.0
        monkeypatch.setenv("VOICE_TURN_BUDGET_SECONDS", "inf")
        assert _voice_turn_budget_s() == 27.0

    def test_below_schema_min_is_floored_to_10(self, monkeypatch):
        """final-review MINOR 1: a sub-10s value (below the schema's min,
        e.g. a direct env override) is floored to 10 defensively rather
        than starving every delegation."""
        from channels.voice.channel import _voice_turn_budget_s
        monkeypatch.setenv("VOICE_TURN_BUDGET_SECONDS", "3")
        assert _voice_turn_budget_s() == 10.0
        monkeypatch.setenv("VOICE_TURN_BUDGET_SECONDS", "0")
        assert _voice_turn_budget_s() == 10.0


# ---------------------------------------------------------------------------
# TestVoiceProgressSink — exactly-once, suppressed after real speech
# ---------------------------------------------------------------------------


class _FakeAgentConfig:
    class tts:
        tag_dialect = "square_brackets"
    memory = type("M", (), {"token_budget": 800})()
    role = "butler"
    voice_errors: dict[str, str] = {}
    channels: list[str] = ["ha_voice"]


class _DummyMemory:
    async def ensure_session(self, *a, **kw): return None
    async def get_context(self, *a, **kw): return ""
    async def add_turn(self, *a, **kw): return None
    async def profile(self, bank: str) -> str: return ""


class _ProgressTwiceAgent:
    """Calls _progress_sink twice — the sink itself must enforce
    exactly-once; a second call must not write a second block."""

    def __init__(self, bus: MessageBus, role: str) -> None:
        self._role = role

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        sink = msg.context.get("_progress_sink")
        if sink:
            await sink("One moment — checking.")
            await sink("One moment — checking.")
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="done", reply_to=msg.id, channel=msg.channel,
            context=msg.context,
        )


class _SpeechThenProgressAgent:
    """Speaks real content via on_token FIRST, then tries to emit
    progress — the sink must suppress once speech_block_sent is True."""

    def __init__(self, bus: MessageBus, role: str) -> None:
        self._role = role

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        on_token = msg.context.get("_on_token")
        sink = msg.context.get("_progress_sink")
        if on_token:
            await on_token("Sure thing.")
        if sink:
            await sink("One moment — checking.")
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="Sure thing.", reply_to=msg.id, channel=msg.channel,
            context=msg.context,
        )


class _RaceAgent:
    """Fires a real on_token speech block and the progress sink so the sink
    is guaranteed to attempt its emission WHILE the real block's write is
    still in flight (holding the lock). This deterministically reproduces
    the exact interleaving the check-outside-lock bug required: the progress
    sink observes speech_block_sent=False, queues behind the lock, and (in
    the buggy code) writes progress AFTER real speech. Under the fix the
    sink's check + write + mutation all happen under the held lock, so it
    observes speech_block_sent=True and suppresses.

    The real-speech write is gated on ``barrier_reached``/``release`` — the
    transport's write hook sets ``barrier_reached`` and awaits ``release``
    while the lock is held, so the test can inject the progress sink into
    that exact window. Because the fix makes the sink BLOCK on the lock, the
    sink is fired as a concurrent task (not awaited) before ``release`` is
    set — awaiting it first would deadlock (sink waits on lock, lock waits
    on release, release waits on sink)."""

    def __init__(self, bus, role, barrier_reached, release):
        self._role = role
        self._barrier_reached = barrier_reached
        self._release = release

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        on_token = msg.context["_on_token"]
        sink = msg.context["_progress_sink"]
        t_speech = asyncio.create_task(on_token("Real speech here."))
        await self._barrier_reached.wait()   # speech mid-write, holding lock
        t_prog = asyncio.create_task(sink("One moment — checking."))
        await asyncio.sleep(0.01)             # let the sink reach the lock
        self._release.set()                  # let the speech write complete
        await asyncio.gather(t_speech, t_prog)
        return BusMessage(
            type=MessageType.RESPONSE, source=self._role, target=msg.source,
            content="Real speech here.", reply_to=msg.id,
            channel=msg.channel, context=msg.context,
        )


class _BarrierWS:
    """Minimal WS stub whose send_json gates the real-speech block write on
    a barrier (mirrors the SSE _write_sse monkeypatch)."""

    def __init__(self, barrier_reached, release):
        self.sent: list[dict] = []
        self._barrier_reached = barrier_reached
        self._release = release

    async def send_json(self, data: dict) -> None:
        if data.get("type") == "block" and "Real speech" in data.get("text", ""):
            self._barrier_reached.set()
            await self._release.wait()
        self.sent.append(data)


async def _parse_sse(response) -> list[dict]:
    frames: list[dict] = []
    async for line in response.content:
        s = line.decode("utf-8").rstrip("\r\n")
        if s.startswith("event:"):
            frames.append({"event": s.split(":", 1)[1].strip()})
        elif s.startswith("data:") and frames:
            frames[-1]["data"] = json.loads(s.split(":", 1)[1].strip())
    return frames


@pytest.mark.asyncio
class TestVoiceProgressSink:
    async def test_progress_sink_writes_block_exactly_once(self):
        bus = MessageBus()
        agent = _ProgressTwiceAgent(bus, "butler")
        bus.register("butler", agent.handle_message)
        loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

        channel = VoiceChannel(
            bus=bus, default_agent="butler", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"butler": _FakeAgentConfig()},
            memory=_DummyMemory(), idle_timeout=300,
        )
        app = web.Application(middlewares=[cid_middleware])
        channel.register_routes(app)
        try:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/converse", json={
                    "prompt": "hi", "agent_role": "butler", "scope_id": "s1",
                })
                frames = await _parse_sse(resp)
        finally:
            loop_task.cancel()

        blocks = [f["data"]["text"] for f in frames if f["event"] == "block"]
        assert blocks.count("One moment — checking.") == 1, blocks

    async def test_progress_sink_suppressed_after_real_speech(self):
        bus = MessageBus()
        agent = _SpeechThenProgressAgent(bus, "butler")
        bus.register("butler", agent.handle_message)
        loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

        channel = VoiceChannel(
            bus=bus, default_agent="butler", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"butler": _FakeAgentConfig()},
            memory=_DummyMemory(), idle_timeout=300,
        )
        app = web.Application(middlewares=[cid_middleware])
        channel.register_routes(app)
        try:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/converse", json={
                    "prompt": "hi", "agent_role": "butler", "scope_id": "s2",
                })
                frames = await _parse_sse(resp)
        finally:
            loop_task.cancel()

        blocks = [f["data"]["text"] for f in frames if f["event"] == "block"]
        assert "One moment — checking." not in blocks, blocks
        assert "Sure thing." in blocks, blocks

    async def test_progress_loses_race_to_inflight_real_speech_sse(
        self, monkeypatch,
    ):
        """IMPORTANT 3 (review): a progress emission that RACES an in-flight
        real-speech block must lose — under the fix (check+write+mutation
        all under the lock) the sink observes speech_block_sent=True and
        suppresses. The old check-outside-lock code emitted progress AFTER
        the real block; this test fails on that code."""
        import channels.voice.channel as vc

        barrier_reached = asyncio.Event()
        release = asyncio.Event()
        orig_write = vc._write_sse

        async def gated_write(response, event, data):
            if event == "block" and "Real speech" in data.get("text", ""):
                barrier_reached.set()
                await release.wait()
            await orig_write(response, event, data)

        monkeypatch.setattr(vc, "_write_sse", gated_write)

        bus = MessageBus()
        agent = _RaceAgent(bus, "butler", barrier_reached, release)
        bus.register("butler", agent.handle_message)
        loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

        channel = VoiceChannel(
            bus=bus, default_agent="butler", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"butler": _FakeAgentConfig()},
            memory=_DummyMemory(), idle_timeout=300,
        )
        app = web.Application(middlewares=[cid_middleware])
        channel.register_routes(app)
        try:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/converse", json={
                    "prompt": "hi", "agent_role": "butler", "scope_id": "race1",
                })
                frames = await _parse_sse(resp)
        finally:
            loop_task.cancel()

        blocks = [f["data"]["text"] for f in frames if f["event"] == "block"]
        assert not any("One moment" in b for b in blocks), blocks
        assert any("Real speech" in b for b in blocks), blocks

    async def test_progress_loses_race_to_inflight_real_speech_ws(self):
        """IMPORTANT 3 (review), WS transport — same race property as the
        SSE test, driven through _run_ws_utterance directly with a barrier
        WS stub."""
        barrier_reached = asyncio.Event()
        release = asyncio.Event()

        bus = MessageBus()
        agent = _RaceAgent(bus, "butler", barrier_reached, release)
        bus.register("butler", agent.handle_message)
        loop_task = asyncio.create_task(bus.run_agent_loop("butler"))

        channel = VoiceChannel(
            bus=bus, default_agent="butler", webhook_secret="",
            sse_path="/api/converse", ws_path="/api/converse/ws",
            agent_configs={"butler": _FakeAgentConfig()},
            memory=_DummyMemory(), idle_timeout=300,
        )
        ws = _BarrierWS(barrier_reached, release)
        try:
            await channel._run_ws_utterance(
                ws,
                {"text": "hi", "agent_role": "butler", "scope_id": "race2"},
                "u1",
                asyncio.get_running_loop().time() + 20.0,
            )
        finally:
            loop_task.cancel()

        blocks = [d["text"] for d in ws.sent if d.get("type") == "block"]
        assert not any("One moment" in b for b in blocks), blocks
        assert any("Real speech" in b for b in blocks), blocks


# ---------------------------------------------------------------------------
# TestVoiceDeadlineOriginPropagation — agent.py _process, voice-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestVoiceDeadlineOriginPropagation:
    async def test_voice_channel_propagates_deadline_and_sink(self, tmp_path):
        import agent as agent_mod
        from test_agent_process import FakeClient, _make_agent

        captured: dict = {}

        class CapturingClient(FakeClient):
            async def receive_response(self):
                captured["origin"] = agent_mod.origin_var.get(None)
                async for m in super().receive_response():
                    yield m

        async def _sink(text: str) -> None:
            return None

        a = _make_agent(tmp_path, role="butler")
        msg = BusMessage(
            type=MessageType.CHANNEL_IN, source="voice", target="butler",
            content="hi", channel="voice",
            context={
                "chat_id": "s1", "_voice_deadline": 12345.0,
                "_progress_sink": _sink,
            },
        )
        with patch("sdk_client_pool._default_make_client", CapturingClient):
            await a._process(msg)

        origin = captured["origin"]
        assert origin["voice_deadline"] == 12345.0
        assert origin["_progress_sink"] is _sink

    async def test_non_voice_channel_does_not_propagate_deadline(self, tmp_path):
        """Defensive: even if a non-voice message somehow carried these
        keys on msg.context, _process must not copy them into origin —
        the gate is msg.channel == 'voice', not key presence."""
        import agent as agent_mod
        from test_agent_process import FakeClient, _make_agent

        captured: dict = {}

        class CapturingClient(FakeClient):
            async def receive_response(self):
                captured["origin"] = agent_mod.origin_var.get(None)
                async for m in super().receive_response():
                    yield m

        async def _sink(text: str) -> None:
            return None

        a = _make_agent(tmp_path, role="assistant")
        msg = BusMessage(
            type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
            content="hi", channel="telegram",
            context={
                "chat_id": "s1", "_voice_deadline": 12345.0,
                "_progress_sink": _sink,
            },
        )
        with patch("sdk_client_pool._default_make_client", CapturingClient):
            await a._process(msg)

        origin = captured["origin"]
        assert "voice_deadline" not in origin
        assert "_progress_sink" not in origin
