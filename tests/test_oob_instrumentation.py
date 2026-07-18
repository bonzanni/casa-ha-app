"""Out-of-band posting instrumentation (spec §D1 / §D7).

A6 owns ONLY the ``slot_missed`` timing case below: it proves the anchor path's
PLACEHOLDER send-intent (registered PENDING, before validation) lets the
sequencer mark the block ``slot_missed`` when validation outlasts the 2 s slot
hold, so the late anchor posts IMMEDIATELY on completion (via the slot_missed
branch of ``process_intents_once``) rather than waiting out the full 10 s intent
timeout — the operator-visible F-OOB delay round 4 only instruments.

REAL ``ClaudeCodeDriver`` + REAL ``OutputSequencer`` (with an INJECTED clock —
never patches ``<module>.asyncio.sleep``, per the memory-cage rule) + REAL
``EngagementRegistry`` + REAL ``VerdictBroker``. The rest of this file is Task
E1's.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from aiohttp import web

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker
from channels.output_sequencer import ASK_TOOL, OutputSequencer, projection_hash

pytestmark = pytest.mark.asyncio

_ANCHOR_HASH = "anchor-hash"

# E1 · F-OOB instrumentation (spec D7) sentinel: an operator/agent BODY string
# that must NEVER appear in any instrumentation log line (content-free,
# like ``floored_ask_telemetry`` — hash-prefix/result/state/latency only).
_SENTINEL_BODY = "SENTINEL-DB-CREDENTIALS-QUESTION-TEXT-2026-07-17"


class _Chan:
    """Minimal ordered fake wire: the anchor poster's plain ``send_to_topic``
    and the sequencer's narration primitives draw from ONE id counter."""

    def __init__(self) -> None:
        self._next = 100
        self.anchors: list[tuple[int, str]] = []

    def _id(self) -> int:
        m = self._next
        self._next += 1
        return m

    async def send_to_topic(self, thread_id, text, **kwargs) -> int | None:
        m = self._id()
        self.anchors.append((m, text))
        return m

    async def post_options_keyboard(self, **kwargs) -> int:
        return self._id()

    async def edit_topic_message(self, *a, **k) -> bool:
        return True

    async def post_ask_body_rich(self, thread_id, text, **kwargs) -> int | None:
        return await self.send_to_topic(thread_id, text, **kwargs)

    async def edit_topic_message_rich(self, *a, **k) -> bool:
        return await self.edit_topic_message(*a, **k)

    async def narration_send(self, topic_id, text, reply_to=None) -> int:
        return self._id()

    async def narration_edit(self, topic_id, message_id, text) -> bool:
        return True


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def _body(resp: web.Response) -> dict:
    return json.loads(resp.text)


class TestSlotMissedTiming:
    async def test_slow_validation_slot_missed_posts_on_completion_not_at_timeout(
        self, tmp_path, monkeypatch,
    ):
        from engagement_registry import EngagementRegistry
        from drivers.claude_code_driver import ClaudeCodeDriver
        from channels.channel_handlers import _make_channel_handlers
        from unittest.mock import AsyncMock

        fresh = VerdictBroker()
        monkeypatch.setattr(verdict_broker, "BROKER", fresh)

        # INJECTED clock: a monotonically-advanced dict, advanced by the
        # sequencer's own ``_sleep`` (so the slot hold expires deterministically
        # WITHOUT touching the global asyncio.sleep — memory-cage rule).
        clock = {"t": 1000.0}

        def _now() -> float:
            return clock["t"]

        async def _sleep(seconds: float) -> None:
            clock["t"] += seconds
            await asyncio.sleep(0)

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "claude_code", "t",
            {"user_id": 555}, topic_id=42)
        chan = _Chan()
        seq = OutputSequencer(
            engagement_id=rec.id, topic_id=42,
            send_message=chan.narration_send, edit_message=chan.narration_edit,
            _now=_now, _sleep=_sleep,
            slot_hold_s=0.2, intent_timeout_s=10.0, hold_poll_s=0.05)
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "eng"),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
            edit_topic_message=chan.edit_topic_message, registry=reg)
        drv._sequencers[rec.id] = seq
        monkeypatch.setattr(agent_mod, "active_claude_code_driver", drv)
        handlers = _make_channel_handlers(
            telegram_channel=chan, engagement_registry=reg)
        ask = handlers["/internal/channel/ask"]

        # Validation (number allocation) is PARKED past the slot hold.
        alloc_gate = asyncio.Event()

        async def _slow_alloc(_e):
            await alloc_gate.wait()
            return 1

        monkeypatch.setattr(reg, "allocate_question_number", _slow_alloc)

        task = asyncio.ensure_future(ask(_FakeRequest(
            {"engagement_id": rec.id, "request_id": "s1",
             "question": "DB name?", "options": [], "timeout_s": 60,
             "projection_hash": _ANCHOR_HASH})))
        await asyncio.sleep(0.02)

        # The placeholder intent is registered PENDING before validation — so the
        # relay's block has a real intent to mark slot_missed.
        intent = seq.registry.by_request_id("s1")
        assert intent is not None and intent.state == "pending"

        # The relay reaches the ask block while validation is still parked: it
        # holds the 2 s slot (the injected clock advances via _sleep) and, on
        # timeout, marks the still-pending intent slot_missed and proceeds.
        res = await seq.post_for_block(ASK_TOOL, _ANCHOR_HASH)
        assert res == "slot_timeout"
        assert intent.slot_missed is True
        assert chan.anchors == []                 # nothing posted yet

        # Validation completes: the handler set_passed + installs/arms the poster.
        alloc_gate.set()
        await asyncio.sleep(0.02)
        assert intent.state == "armed" and intent.slot_missed is True
        assert chan.anchors == []                 # arm alone does not post

        # The clock is nowhere near the 10 s intent timeout — the post is driven
        # by slot_missed, NOT the timeout.
        assert _now() - intent.registered_at < seq._intent_timeout_s
        await seq.process_intents_once()
        assert len(chan.anchors) == 1             # posted IMMEDIATELY via slot_missed

        resp = await asyncio.wait_for(task, timeout=1.0)
        assert _body(resp)["outcome"] == "anchored"


# ---------------------------------------------------------------------------
# Task E1 · F-OOB instrumentation (spec D7): match-point + late-post-watcher
# INFO logs. LOGS ONLY — no behavioral change. REAL ``OutputSequencer`` +
# REAL ``IntentRegistry`` + REAL ``TopicStreamRelay._match_discrete_block``,
# fake wire send/edit fns, injected clock (never patches
# ``<module>.asyncio.sleep`` — the memory-cage rule).
# ---------------------------------------------------------------------------


def _clock(start: float = 1000.0):
    """A monotonically-advanced injected clock (dict + closures), advanced by
    the sequencer's/relay's own ``_sleep`` — never the global ``asyncio.sleep``."""
    state = {"t": start}

    def _now() -> float:
        return state["t"]

    async def _sleep(seconds: float) -> None:
        state["t"] += seconds
        await asyncio.sleep(0)

    return _now, _sleep


def _make_relay(tmp_path, chan, seq, _now, _sleep):
    from drivers.topic_stream import TopicStreamRelay

    async def _noop_delete(*_a, **_k) -> bool:
        return True

    return TopicStreamRelay(
        engagement_id=seq.engagement_id, topic_id=seq.topic_id,
        log_dir=str(tmp_path / "log"), cursor_path=str(tmp_path / "cursor.json"),
        send_message=chan.narration_send, edit_message=chan.narration_edit,
        delete_message=_noop_delete,
        on_turn_event=lambda *a, **k: None, reply_texts=lambda: set(),
        sequencer=seq, _now=_now, _sleep=_sleep,
    )


class TestMatchedPostLogging:
    """RED: a matched (armed → posted) block logs all four D7 dimensions —
    hash-prefix, block-resolution result, intent state, and the
    registration-to-block latency — with NO body text anywhere in the line."""

    async def test_matched_post_logs_all_four_dimensions(self, tmp_path, caplog):
        chan = _Chan()
        _now, _sleep = _clock()
        seq = OutputSequencer(
            engagement_id="eng-oob-matched", topic_id=42,
            send_message=chan.narration_send, edit_message=chan.narration_edit,
            _now=_now, _sleep=_sleep)
        relay = _make_relay(tmp_path, chan, seq, _now, _sleep)

        raw_args = {
            "question": _SENTINEL_BODY, "options": [], "timeout_s": 60,
            "multi": False,
        }
        block_hash = projection_hash(ASK_TOOL, raw_args)

        posted: list[int] = []

        async def _poster():
            mid = await chan.narration_send(42, "keyboard")
            posted.append(mid)
            return mid

        seq.register_intent(
            request_id="m1", tool_name=ASK_TOOL, projection_hash=block_hash,
            poster=_poster)
        seq.arm_intent("m1")

        caplog.set_level(logging.INFO)
        await relay._match_discrete_block(ASK_TOOL, raw_args)

        assert len(posted) == 1  # sanity: the block actually matched+posted
        text = caplog.text
        assert block_hash[:8] in text
        assert "result=posted" in text
        assert "intent_state=" in text
        assert "latency_ms=" in text
        assert _SENTINEL_BODY not in text


class TestSlotTimeoutLatePostLogging:
    """RED: a ``slot_timeout`` match followed by its eventual out-of-band
    (late) post logs timing sufficient to reconstruct the F-OOB ~10s gap —
    the match-point log (``slot_timeout``, latency ~= the slot hold) and the
    late-post-watcher log (``posted``, latency covering the FULL registration-
    to-actual-post span) together bound the gap. No body text in either
    line."""

    async def test_slot_timeout_then_late_post_logs_reconstructable_timing(
        self, tmp_path, caplog,
    ):
        chan = _Chan()
        _now, _sleep = _clock()
        seq = OutputSequencer(
            engagement_id="eng-oob-late", topic_id=42,
            send_message=chan.narration_send, edit_message=chan.narration_edit,
            _now=_now, _sleep=_sleep,
            slot_hold_s=0.2, intent_timeout_s=10.0, hold_poll_s=0.05)
        relay = _make_relay(tmp_path, chan, seq, _now, _sleep)

        raw_args = {
            "question": _SENTINEL_BODY, "options": [], "timeout_s": 60,
            "multi": False,
        }
        block_hash = projection_hash(ASK_TOOL, raw_args)

        posted: list[int] = []

        async def _poster():
            mid = await chan.narration_send(42, "keyboard")
            posted.append(mid)
            return mid

        # The intent is registered PENDING — validation has not yet armed it —
        # so the relay's block match holds the slot then times out.
        seq.register_intent(
            request_id="s1", tool_name=ASK_TOOL, projection_hash=block_hash,
            poster=_poster)

        caplog.set_level(logging.INFO)
        await relay._match_discrete_block(ASK_TOOL, raw_args)
        first_latency = _now() - seq.registry.by_request_id("s1").registered_at
        assert posted == []  # nothing posted yet — still pending

        match_text = caplog.text
        assert f"hash={block_hash[:8]}" in match_text
        assert "result=slot_timeout" in match_text
        assert "intent_state=pending" in match_text
        assert "latency_ms=" in match_text
        assert _SENTINEL_BODY not in match_text

        # Validation completes well after the slot timed out — the relay's
        # background watcher tick posts the now-armed, slot_missed intent
        # out-of-band. Advance the clock to simulate the elapsed gap.
        await _sleep(0.5)
        seq.arm_intent("s1")
        caplog.clear()
        await seq.process_intents_once()
        assert len(posted) == 1  # the late post landed

        late_text = caplog.text
        assert f"hash={block_hash[:8]}" in late_text
        assert "result=posted" in late_text
        assert "reason=slot_missed" in late_text
        assert "latency_ms=" in late_text
        assert _SENTINEL_BODY not in late_text

        # The two logged latencies bound the actual gap: the late post's
        # registration-to-post span is strictly larger than the match-point
        # span recorded when the block first timed out — together they
        # reconstruct the observed F-OOB delay (Sol r1-8: slot hold + however
        # long validation/registration actually took).
        late_intent = seq.registry.by_request_id("s1")
        second_latency = _now() - late_intent.registered_at
        assert second_latency > first_latency
        assert second_latency >= 0.5


# ---------------------------------------------------------------------------
# Task 6 · R6a: oob_match log LEVELS. Every tool_use block drives
# ``_match_discrete_block`` (§2(3)), but only HOLD-ELIGIBLE post tools
# (ask/reply/emit_completion) can ever resolve to anything other than
# ``no_match latency=-1.0`` — every other block (TaskCreate/TaskUpdate/
# ToolSearch/Glob/...) logs a guaranteed-uninteresting no_match at the SAME
# INFO level today, burying the real F-OOB ``oob_late_post``/``slot_timeout``
# signal. Keep INFO for the hold-eligible set (do NOT drop emit_completion —
# it is the D7-instrumented completion ingress), DEBUG everything else. LOGS
# ONLY — no behavioral change to matching/posting.
# ---------------------------------------------------------------------------


class TestOobLogLevels:
    async def test_hold_eligible_post_tools_log_oob_match_at_info(
        self, tmp_path, caplog,
    ):
        from channels.output_sequencer import REPLY_TOOL, EMIT_COMPLETION_TOOL

        chan = _Chan()
        _now, _sleep = _clock()
        seq = OutputSequencer(
            engagement_id="eng-oob-levels-info", topic_id=42,
            send_message=chan.narration_send, edit_message=chan.narration_edit,
            _now=_now, _sleep=_sleep)
        relay = _make_relay(tmp_path, chan, seq, _now, _sleep)

        caplog.set_level(logging.DEBUG)
        for tool_name, raw_args in (
            (ASK_TOOL, {"question": "q?", "options": [], "timeout_s": 60,
                        "multi": False}),
            (REPLY_TOOL, {"text": "hi"}),
            (EMIT_COMPLETION_TOOL, {"summary": "done"}),
        ):
            caplog.clear()
            await relay._match_discrete_block(tool_name, raw_args)
            oob_records = [
                r for r in caplog.records if r.getMessage().startswith("oob_match")
            ]
            assert oob_records, f"no oob_match record logged for {tool_name}"
            assert all(r.levelno == logging.INFO for r in oob_records), (
                f"{tool_name} oob_match logged below INFO: "
                f"{[r.levelname for r in oob_records]}"
            )

    async def test_non_post_tool_block_logs_oob_match_at_debug(
        self, tmp_path, caplog,
    ):
        chan = _Chan()
        _now, _sleep = _clock()
        seq = OutputSequencer(
            engagement_id="eng-oob-levels-debug", topic_id=42,
            send_message=chan.narration_send, edit_message=chan.narration_edit,
            _now=_now, _sleep=_sleep)
        relay = _make_relay(tmp_path, chan, seq, _now, _sleep)

        # A non-post tool block can NEVER match a hold-eligible intent — its
        # oob_match is always a guaranteed-uninteresting
        # ``no_match latency=-1.0``.
        for tool_name in ("Glob", "TaskCreate"):
            caplog.clear()
            caplog.set_level(logging.DEBUG)
            await relay._match_discrete_block(tool_name, {"pattern": "*.py"})
            oob_records = [
                r for r in caplog.records if r.getMessage().startswith("oob_match")
            ]
            assert oob_records, f"no oob_match record logged for {tool_name}"
            assert all(r.levelno == logging.DEBUG for r in oob_records), (
                f"{tool_name} oob_match not DEBUG: "
                f"{[r.levelname for r in oob_records]}"
            )

            # At the real production log level (INFO), the record must not
            # surface at all — this is the actual "not buried" assertion.
            caplog.clear()
            caplog.set_level(logging.INFO)
            await relay._match_discrete_block(tool_name, {"pattern": "*.py"})
            info_and_above = [
                r for r in caplog.records if r.getMessage().startswith("oob_match")
            ]
            assert info_and_above == [], (
                f"{tool_name} oob_match still visible at INFO: {info_and_above}"
            )


# ---------------------------------------------------------------------------
# Task 6 · R6b: regression test ONLY — no implementation change. The edge
# (anchor armed → a tool_use block with NO prose in between → post-tool
# prose) is already handled by ``_flush_and_disarm`` (§D5 r23-2): a tool_use
# block flushes any buffered prose and disarms suppression for the rest of
# the turn EVEN WHEN THE BUFFER IS EMPTY (``_flush_anchor_buffer`` returns
# ``True`` immediately on an empty buffer), so post-tool-use prose is never
# suppressed even though no prose was ever buffered to flush. This guards
# that behavior against regression.
# ---------------------------------------------------------------------------


class TestAnchorToolUseNoProsePostToolProseStaysVisible:
    async def test_post_tool_prose_after_bare_tool_use_stays_visible(
        self, tmp_path,
    ):
        chan = _Chan()
        # Track POSTED narration text (the real wire the relay's ordinary
        # prose flows through — distinct from ``chan.anchors``, which only
        # ``send_to_topic``/``post_ask_body_rich`` populate) BEFORE building
        # the sequencer, so it captures the wrapped send.
        posted: list[str] = []
        _orig_send = chan.narration_send

        async def _tracking_send(topic_id, text, reply_to=None) -> int:
            posted.append(text)
            return await _orig_send(topic_id, text, reply_to=reply_to)

        chan.narration_send = _tracking_send

        _now, _sleep = _clock()
        seq = OutputSequencer(
            engagement_id="eng-r6b", topic_id=42,
            send_message=chan.narration_send, edit_message=chan.narration_edit,
            _now=_now, _sleep=_sleep)
        relay = _make_relay(tmp_path, chan, seq, _now, _sleep)

        raw_args = {
            "question": "Continue?", "options": [], "timeout_s": 60,
            "multi": False,
        }
        block_hash = projection_hash(ASK_TOOL, raw_args)

        # 1) The anchor's own ask block matches (no_match — no intent
        #    registered), which records the per-turn anchor candidate.
        await relay._match_discrete_block(ASK_TOOL, raw_args)
        assert relay._anchor_candidate == (ASK_TOOL, block_hash)

        # 2) Simulate the anchor surfacing as open (the driver-injected seam)
        #    bound to THIS candidate's own hash, and arm suppression — the
        #    same state a genuinely-open anchor produces.
        relay.open_anchor_state = lambda: (1, 900, block_hash)
        await relay._maybe_arm_suppression()
        assert relay._suppressing_for is not None

        # 3) A tool_use block arrives with NO prose buffered in between
        #    (the anchor buffer is empty). Per §D5 r23-2 this flushes
        #    (trivially — nothing to flush) and DISARMS.
        await relay._handle_assistant_blocks(
            [("tool_use", "Glob", {"pattern": "*.py"})],
            seg=(0, 0), off_after=10, message_id="m1",
        )
        assert relay._suppressing_for is None
        assert relay._anchor_candidate is None

        # 4) Post-tool-use prose must post as ORDINARY narration — visible,
        #    not buffered/suppressed.
        await relay._handle_assistant_blocks(
            [("text", "Here is the result.")],
            seg=(0, 20), off_after=20, message_id="m2",
        )
        assert relay._anchor_buffer == []
        assert any("Here is the result." in text for text in posted)
