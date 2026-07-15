"""Tests for ``channels.output_sequencer`` — the per-topic OUTPUT SEQUENCER +
relay-mediated discrete-posting intent registry (v0.79.0 Primitive A, design §2).

Every §2 sentence is binding; these exercise the machinery in isolation with
injected async send/edit recorders and an injected clock. Time is injected — the
slot-hold loop terminates because the fake ``_sleep`` advances the fake clock, so
we never patch ``asyncio.sleep`` (the global-patch OOM lesson).
"""
from __future__ import annotations

import asyncio

from channels.output_sequencer import (
    APPLIED,
    ASK_TOOL,
    EMIT_COMPLETION_TOOL,
    FAILED,
    MARKUP_EMPTY,
    REPLY_TOOL,
    SEALED,
    IntentRegistry,
    OutputSequencer,
    project_args,
    projection_hash,
)


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.sends: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self._next_id = 100
        self.edit_fails = 0

    async def send(self, topic_id: int, text: str) -> int | None:
        self.sends.append((topic_id, text))
        mid = self._next_id
        self._next_id += 1
        return mid

    async def edit(self, topic_id: int, message_id: int, text: str) -> bool:
        if self.edit_fails > 0:
            self.edit_fails -= 1
            return False
        self.edits.append((topic_id, message_id, text))
        return True


class Clock:
    """Monotonic fake clock; ``sleep`` advances it so hold loops terminate."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, dt: float) -> None:
        self.t += dt


def _make_seq(rec, clock, **kw):
    return OutputSequencer(
        engagement_id="eng-1",
        topic_id=42,
        send_message=rec.send,
        edit_message=rec.edit,
        _now=clock.now,
        _sleep=clock.sleep,
        slot_hold_s=2.0,
        intent_timeout_s=10.0,
        hold_poll_s=0.05,
        **kw,
    )


def _poster(rec, text):
    async def _post():
        return await rec.send(42, text)
    return _post


# ---------------------------------------------------------------------------
# Projection / hash.
# ---------------------------------------------------------------------------


def test_project_args_pins_reply_to_text_only():
    assert project_args(REPLY_TOOL, {"chat_id": "x", "text": "hi"}) == {"text": "hi"}
    assert project_args(ASK_TOOL, {"question": "q", "options": ["a", "b"],
                                   "timeout_s": 300, "extra": 1}) == {
        "question": "q", "options": ["a", "b"], "timeout_s": 300,
    }
    # identity for a gated tool / emit_completion.
    assert project_args("Bash", {"command": "ls"}) == {"command": "ls"}


def test_projection_hash_ignores_reply_chat_id():
    a = projection_hash(REPLY_TOOL, {"chat_id": "1", "text": "same"})
    b = projection_hash(REPLY_TOOL, {"chat_id": "2", "text": "same"})
    assert a == b
    c = projection_hash(REPLY_TOOL, {"text": "different"})
    assert c != a


# ---------------------------------------------------------------------------
# Narration: open / edit-if-latest / seal / no-op gate.
# ---------------------------------------------------------------------------


async def test_edit_narration_if_latest_applies_then_seals_on_interleave():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    mid = await seq.open_narration("hello")
    assert mid == 100 and seq.narration_msg_id == 100 and seq.high_water == 100
    # Still latest → edit applies.
    assert await seq.edit_narration_if_latest(mid, "hello world") == APPLIED
    assert rec.edits[-1] == (42, 100, "hello world")
    # A discrete post below seals narration → subsequent edit returns SEALED.
    h = projection_hash(REPLY_TOOL, {"text": "R"})
    seq.register_intent(request_id="r1", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "R"))
    seq.arm_intent("r1")
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert seq.narration_msg_id is None  # rollover-on-interleave sealed it
    assert await seq.edit_narration_if_latest(mid, "hello world!") == SEALED


async def test_noop_edit_gate_skips_identical_and_retries_after_failure():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    mid = await seq.open_narration("x")
    # open cached (text="x", absent); identical edit is a no-op skip.
    assert await seq.edit_narration_if_latest(mid, "x") == APPLIED
    assert rec.edits == []  # skipped, never hit the wire
    # A distinct edit that FAILS invalidates the cache so a retry is not
    # suppressed even though its text/markup matches the failed attempt.
    rec.edit_fails = 1
    assert await seq.edit_narration_if_latest(mid, "y") == FAILED
    assert await seq.edit_narration_if_latest(mid, "y") == APPLIED
    assert rec.edits[-1] == (42, mid, "y")


async def test_markup_tristate_distinguishes_empty_from_absent():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    mid = await seq.open_narration("q")  # cached as (q, absent)
    # Same text but an explicit-empty markup is NOT a no-op (Sol r2-2): a
    # markup-only settlement must still fire.
    assert await seq.edit_narration_if_latest(mid, "q", markup=MARKUP_EMPTY) == APPLIED
    assert rec.edits[-1] == (42, mid, "q")
    # Now identical (q, empty) IS a no-op.
    rec.edits.clear()
    assert await seq.edit_narration_if_latest(mid, "q", markup=MARKUP_EMPTY) == APPLIED
    assert rec.edits == []


async def test_inbound_advance_seals_narration():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    mid = await seq.open_narration("mid-turn narration")
    await seq.advance_high_water_for_inbound(operator_msg_id=555)
    assert seq.narration_msg_id is None
    assert seq.high_water == 555
    assert await seq.edit_narration_if_latest(mid, "late narration") == SEALED


# ---------------------------------------------------------------------------
# Intent matching at content-block positions.
# ---------------------------------------------------------------------------


async def test_non_hold_eligible_block_without_intent_is_no_match_instantly():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash("Bash", {"command": "ls"})
    assert await seq.post_for_block("Bash", h) == "no_match"
    assert clock.t == 0.0  # never held


async def test_armed_intent_posts_at_block():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "hi"})
    intent, created = seq.register_intent(
        request_id="r1", tool_name=REPLY_TOOL, projection_hash=h,
        poster=_poster(rec, "hi"))
    assert created
    seq.arm_intent("r1")
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert rec.sends == [(42, "hi")]
    assert intent.message_id == 100
    assert intent.outcome == {"ok": True, "message_id": 100, "out_of_band": False}


async def test_identical_consecutive_replies_both_post_no_dedup():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "same"})
    seq.register_intent(request_id="r1", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "same"))
    seq.register_intent(request_id="r2", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "same"))
    seq.arm_intent("r1")
    seq.arm_intent("r2")
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert rec.sends == [(42, "same"), (42, "same")]  # duplicates preferred


async def test_cancelled_first_valid_second_same_projection():
    """§2(3): a tombstone consumes block 1; the valid intent binds block 2 —
    cancelled-first/valid-second poisoning is structurally closed."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "dup"})
    seq.register_intent(request_id="bad", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "dup"))
    seq.register_intent(request_id="good", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "dup"))
    seq.cancel_intent("bad")   # tombstone
    seq.arm_intent("good")
    # Block 1 binds the OLDEST matchable (the tombstone) → consumed-cancelled.
    assert await seq.post_for_block(REPLY_TOOL, h) == "consumed_cancelled"
    assert rec.sends == []
    # Block 2 binds the valid intent.
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert rec.sends == [(42, "dup")]


async def test_reversed_arrival_distinct_payloads_via_slot_hold():
    """§2(4): handler B reaches casa-main first (B armed before A registers);
    stream order is block A then block B. The slot hold on block A waits for A
    to arm, so A posts at block A and B at block B — stream order preserved."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    hA = projection_hash(REPLY_TOOL, {"text": "A"})
    hB = projection_hash(REPLY_TOOL, {"text": "B"})
    # B registers + arms first.
    seq.register_intent(request_id="B", tool_name=REPLY_TOOL,
                        projection_hash=hB, poster=_poster(rec, "B"))
    seq.arm_intent("B")

    # Relay reads block A first. A is absent → HOLD. Register+arm A "during"
    # the hold by scheduling it on the first poll via a custom sleep.
    async def sleep_then_arm_A(dt):
        clock.t += dt
        if not seq.registry.by_request_id("A"):
            seq.register_intent(request_id="A", tool_name=REPLY_TOOL,
                                projection_hash=hA, poster=_poster(rec, "A"))
            seq.arm_intent("A")
    seq._sleep = sleep_then_arm_A

    assert await seq.post_for_block(REPLY_TOOL, hA) == "posted"
    assert await seq.post_for_block(REPLY_TOOL, hB) == "posted"
    assert rec.sends == [(42, "A"), (42, "B")]  # A before B, stream order


async def test_slot_timeout_late_intent_posts_out_of_band_threaded():
    """§2(4): a pending intent held past the 2s slot is marked slot_missed; the
    relay proceeds; the late intent then posts out-of-band on arrival (the one
    documented, bounded R2 weakening) — no warn, no debt."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(ASK_TOOL, {"question": "q", "options": ["a", "b"],
                                   "timeout_s": 300})
    seq.register_intent(request_id="q1", tool_name=ASK_TOOL,
                        projection_hash=h, poster=_poster(rec, "Q"))
    # pending (not armed) → held then slot times out.
    assert await seq.post_for_block(ASK_TOOL, h) == "slot_timeout"
    assert seq.registry.by_request_id("q1").slot_missed is True
    assert rec.sends == []
    # It arms later; the watcher pass posts it out-of-band threaded.
    seq.arm_intent("q1")
    await seq.process_intents_once()
    assert rec.sends == [(42, "Q")]
    assert seq.registry.by_request_id("q1").outcome["out_of_band"] is True


async def test_intent_timeout_warns_and_leaves_consumption_debt(caplog):
    """§2(5): timeout-post A → late block A consumes the debt → same-hash
    intent B binds block B exactly once."""
    import logging
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "dup"})
    seq.register_intent(request_id="A", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "A"))
    seq.arm_intent("A")
    # 10s pass with no block for A → out-of-band WARN post + a debt tombstone.
    clock.t = 10.0
    with caplog.at_level(logging.WARNING):
        await seq.process_intents_once()
    assert rec.sends == [(42, "A")]
    assert any("out-of-band" in r.message for r in caplog.records)
    a = seq.registry.by_request_id("A")
    assert a.timeout_posted is True and a.matchable() is True  # debt live

    # A second same-hash intent B registers+arms AFTER the timeout post.
    seq.register_intent(request_id="B", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "B"))
    seq.arm_intent("B")
    # Block A arrives late → consumes the DEBT silently (not B).
    assert await seq.post_for_block(REPLY_TOOL, h) == "debt_consumed"
    assert rec.sends == [(42, "A")]  # nothing new
    # Block B binds B exactly once.
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert rec.sends == [(42, "A"), (42, "B")]


async def test_response_loss_after_post_reattaches_without_double_post():
    """§2(1): a transport retry whose request_id matches an already-posted
    intent reattaches idempotently and reads the recorded outcome (incl. the
    posted message id) — no second post, no second frame consumed."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "hi"})
    intent, created = seq.register_intent(
        request_id="r1", tool_name=REPLY_TOOL, projection_hash=h,
        poster=_poster(rec, "hi"))
    assert created
    seq.arm_intent("r1")
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert rec.sends == [(42, "hi")]
    # Response lost after post → transport retry re-registers the SAME id.
    reattached, created2 = seq.register_intent(
        request_id="r1", tool_name=REPLY_TOOL, projection_hash=h,
        poster=_poster(rec, "hi"))
    assert created2 is False
    assert reattached is intent
    assert seq.intent_outcome("r1") == {"ok": True, "message_id": 100,
                                        "out_of_band": False}
    assert rec.sends == [(42, "hi")]  # NO second post


async def test_pre_consumed_block_not_rebound_by_late_same_hash_intent():
    """§2(3): a ``posted`` intent is retired from matching — a late same-hash
    intent binds only its OWN block, never the pre-consumed (retained) one."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "x"})
    a, _ = seq.register_intent(request_id="A", tool_name=REPLY_TOOL,
                               projection_hash=h, poster=_poster(rec, "x"))
    seq.arm_intent("A")
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"  # A consumed
    assert a.matchable() is False  # retired
    # A late same-hash intent B arms; its block binds B, not the retired A.
    b, _ = seq.register_intent(request_id="B", tool_name=REPLY_TOOL,
                               projection_hash=h, poster=_poster(rec, "x2"))
    seq.arm_intent("B")
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert b.message_id is not None and b.message_id != a.message_id
    assert rec.sends == [(42, "x"), (42, "x2")]


async def test_prune_turn_clears_registry():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "x"})
    seq.register_intent(request_id="r1", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "x"))
    seq.prune_turn()
    assert seq.registry.by_request_id("r1") is None


# ---------------------------------------------------------------------------
# IntentRegistry ordering unit.
# ---------------------------------------------------------------------------


def test_registry_oldest_matchable_is_fifo_on_equal_hash():
    reg = IntentRegistry(_now=lambda: 0.0)
    reg.register(request_id="a", tool_name=REPLY_TOOL, projection_hash="H", poster="a")
    reg.register(request_id="b", tool_name=REPLY_TOOL, projection_hash="H", poster="b")
    first = reg.oldest_matchable(REPLY_TOOL, "H")
    assert first.request_id == "a"
    first.consumed = True
    assert reg.oldest_matchable(REPLY_TOOL, "H").request_id == "b"


# ---------------------------------------------------------------------------
# v0.79.0 (§3, Primitive B) — reply-threading of the turn's first post.
# ---------------------------------------------------------------------------


class _ThreadRecorder:
    """Send recorder that records the reply_to target (3-arg send)."""

    def __init__(self) -> None:
        self.sends: list[tuple[int, str, "int | None"]] = []
        self._next_id = 200

    async def send(self, topic_id, text, reply_to=None):
        self.sends.append((topic_id, text, reply_to))
        mid = self._next_id
        self._next_id += 1
        return mid

    async def edit(self, topic_id, message_id, text):
        return True


async def test_turn_first_narration_threads_to_inbound_then_clears():
    rec = _ThreadRecorder()
    clock = Clock()
    seq = _make_seq(rec, clock)
    # Delivery of an inbound envelope sets the turn's reply-thread target.
    seq.set_turn_reply_to(555)
    await seq.open_narration("first line of the turn")
    # The FIRST post threads to the operator's message.
    assert rec.sends[0] == (42, "first line of the turn", 555)
    # A SECOND post this turn is NOT a reply (target consumed once).
    seq._narration_msg_id = None      # force a fresh open
    await seq.open_narration("second line")
    assert rec.sends[1] == (42, "second line", None)


async def test_consume_turn_reply_to_is_one_shot():
    rec = _ThreadRecorder()
    clock = Clock()
    seq = _make_seq(rec, clock)
    seq.set_turn_reply_to(777)
    assert seq.consume_turn_reply_to() == 777
    assert seq.consume_turn_reply_to() is None      # cleared


async def test_prune_turn_clears_unconsumed_reply_anchor(caplog):
    """Review M1: the causal-handoff one-shot anchor "expires at turn end". A
    button answer that continued the turn but produced NO output leaves the
    anchor set; ``prune_turn`` (the turn-finalize path) MUST clear it so it does
    not leak into the next turn and mis-thread that turn's first message."""
    rec = _ThreadRecorder()
    clock = Clock()
    seq = _make_seq(rec, clock)
    # Anchor set (button answer), but the turn produced no output.
    seq.set_turn_reply_to(4242)
    # Turn ends → prune. The anchor must NOT survive.
    seq.prune_turn()
    assert seq.consume_turn_reply_to() is None
    # Next turn's FIRST message is UNTHREADED (2-arg send, no reply_to target).
    await seq.open_narration("next turn line one")
    assert rec.sends[0] == (42, "next turn line one", None)


async def test_no_reply_target_keeps_two_arg_send():
    # With no inbound target, open_narration uses the 2-arg send (back-compat
    # with the T1 Recorder that has no reply_to parameter).
    rec = Recorder()
    clock = Clock()
    seq = _make_seq(rec, clock)
    await seq.open_narration("hi")
    assert rec.sends == [(42, "hi")]


# ---------------------------------------------------------------------------
# v0.79.0 (§4) — eager out-of-band post leaves a consumption debt so the relay
# debt-consumes the ask/reply block (sealing narration, no double post) and a
# retry reattaches to the recorded outcome.
# ---------------------------------------------------------------------------


async def test_mark_intent_posted_leaves_debt_relay_consumes_block():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    await seq.open_narration("narration before the ask")
    h = projection_hash(ASK_TOOL, {"question": "q", "options": ["a", "b"],
                                   "timeout_s": None})
    # Ingress registers the intent, the handler posts eagerly, then records it.
    seq.register_intent(request_id="a1", tool_name=ASK_TOOL,
                        projection_hash=h, poster=_poster(rec, "unused"))
    intent = await seq.mark_intent_posted("a1", 777)
    assert intent.state == "posted" and intent.message_id == 777
    assert seq.high_water == 777
    # The relay reaching the ask block DEBT-CONSUMES it (no second post) and
    # seals the open narration at that position.
    assert await seq.post_for_block(ASK_TOOL, h) == "debt_consumed"
    assert seq.narration_msg_id is None  # narration sealed
    # No extra sends beyond the one narration open (the eager post is external).
    assert len(rec.sends) == 1
    # Retry reattachment: the recorded outcome carries the posted message id.
    assert seq.intent_outcome("a1") == {
        "ok": True, "message_id": 777, "out_of_band": True}


# ---------------------------------------------------------------------------
# §5 R1 exception: edit_summary is a NON-narration edit.
# ---------------------------------------------------------------------------


async def test_edit_summary_does_not_touch_narration_or_high_water():
    """The pinned SUMMARY (§5) lives ABOVE the causal log: editing it must not
    seal the open narration, advance the high-water, or seal the summary
    itself — so T1 narration invariants are preserved."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    # The summary is posted BEFORE any narration (lowest id); simulate id 500.
    summary_id = 500
    nid = await seq.open_narration("live narration")
    assert seq.high_water == nid and seq.narration_msg_id == nid

    assert await seq.edit_summary(summary_id, "goal\n⚙️ working") == APPLIED
    # High-water + open narration are UNTOUCHED by the summary edit.
    assert seq.high_water == nid
    assert seq.narration_msg_id == nid
    # The narration is still editable-if-latest (not sealed by the summary edit).
    assert await seq.edit_narration_if_latest(nid, "live narration more") == APPLIED


async def test_edit_summary_noop_gate_and_failed_invalidation():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    assert await seq.edit_summary(500, "text-A") == APPLIED
    assert len(rec.edits) == 1
    # Identical edit is a no-op skip (still APPLIED, no new wire edit).
    assert await seq.edit_summary(500, "text-A") == APPLIED
    assert len(rec.edits) == 1
    # A failed edit invalidates the cache so a retry is never suppressed.
    rec.edit_fails = 1
    assert await seq.edit_summary(500, "text-B") == FAILED
    assert await seq.edit_summary(500, "text-B") == APPLIED
    assert rec.edits[-1] == (42, 500, "text-B")


# ---------------------------------------------------------------------------
# F1(b) — platform-origin discrete send (receipt/notice) through the writer.
# ---------------------------------------------------------------------------


async def test_platform_notice_seals_narration_below_it():
    """F1(b): a receipt/notice posted through the single writer SEALS open
    narration — a direct receipt posting below narration while
    ``edit_narration_if_latest`` still returns APPLIED is now impossible."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    nar = await seq.open_narration("working on it ")
    assert await seq.edit_narration_if_latest(nar, "working on it more") == APPLIED
    # A platform notice (receipt) posts through the writer → seals narration.
    notice_mid = await seq.post_platform_notice("📥 Received")
    assert notice_mid is not None and notice_mid > nar          # posted BELOW
    # Narration is now SEALED: a further edit can NOT land as APPLIED (it would
    # edit a message with the receipt below it).
    assert await seq.edit_narration_if_latest(nar, "sneaky append") == SEALED


# ---------------------------------------------------------------------------
# F4 — turn-end flush of a late armed intent before prune.
# ---------------------------------------------------------------------------


async def test_flush_armed_intents_posts_before_prune():
    """F4: an armed-but-unposted late intent (its block never arrived) must POST
    out-of-band at turn end, before prune drops it — never silently vanish."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    seq.register_intent(
        request_id="x1", tool_name=REPLY_TOOL, projection_hash="h",
        poster=_poster(rec, "late reply"))
    seq.arm_intent("x1")
    # No post_for_block ever runs (the block never arrived). Turn end flushes it.
    await seq.flush_armed_intents()
    assert (42, "late reply") in rec.sends
    outcome = seq.intent_outcome("x1")
    assert outcome is not None and outcome["ok"] is True
    # Prune signals resolution + clears the registry.
    seq.prune_turn()
    assert seq.registry.by_request_id("x1") is None


# ---------------------------------------------------------------------------
# F6 — drain_and_prune_turn: locked drain + prune (no stale-snapshot drop).
# ---------------------------------------------------------------------------


async def test_drain_and_prune_posts_intent_armed_during_drain():
    """F6: an intent B registered+armed by a late ingress DURING the drain's
    poster await must still POST — never be dropped from a stale armed snapshot
    (the old flush→prune sequence pruned it before it posted)."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)

    async def _a_poster():
        # A late ingress lands mid-post (after any armed snapshot): register +
        # arm intent B while the drain holds the lock.
        seq.register_intent(
            request_id="B", tool_name=REPLY_TOOL, projection_hash="hb",
            poster=_poster(rec, "reply B"))
        seq.arm_intent("B")
        return await rec.send(42, "reply A")

    seq.register_intent(
        request_id="A", tool_name=REPLY_TOOL, projection_hash="ha",
        poster=_a_poster)
    seq.arm_intent("A")

    await seq.drain_and_prune_turn()

    # BOTH posted — B caught by the re-snapshot loop, not silently dropped.
    assert (42, "reply A") in rec.sends
    assert (42, "reply B") in rec.sends
    # Registry fully pruned afterwards.
    assert seq.registry.by_request_id("A") is None
    assert seq.registry.by_request_id("B") is None


async def test_drain_and_prune_seals_narration_and_signals_awaiters():
    """drain_and_prune seals open narration and unblocks any awaiter."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    nar = await seq.open_narration("live narration")
    assert seq.narration_msg_id == nar
    # A pending (never-armed) intent with an awaiter waiting.
    seq.register_intent(
        request_id="p1", tool_name=REPLY_TOOL, projection_hash="h",
        poster=_poster(rec, "x"))
    waiter = asyncio.ensure_future(seq.await_intent_resolution("p1", timeout=5.0))
    await asyncio.sleep(0)
    await seq.drain_and_prune_turn()
    # Narration sealed, registry pruned, awaiter released (None outcome).
    assert seq.narration_msg_id is None
    assert seq.registry.by_request_id("p1") is None
    assert await asyncio.wait_for(waiter, timeout=1.0) is None


# ---------------------------------------------------------------------------
# F3 — await_completion_drain: block until the emit_completion debt is consumed.
# ---------------------------------------------------------------------------


def _register_completion_debt(seq, phash="hc"):
    # A consumption debt is never POSTED (its block is consumed silently), so a
    # placeholder poster is fine — it is never invoked.
    intent, _ = seq.register_intent(
        request_id="emit_completion:eng-1", tool_name=EMIT_COMPLETION_TOOL,
        projection_hash=phash, poster="debt")
    intent.state = "posted"
    intent.timeout_posted = True
    intent.consumed = False
    return intent


async def test_await_completion_drain_waits_for_debt_consumption():
    """F3: the completion drain blocks until the relay CONSUMES the
    emit_completion debt (reaches its block ⇒ all prior frames processed)."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    _register_completion_debt(seq)

    drain = asyncio.ensure_future(
        seq.await_completion_drain("emit_completion:eng-1", timeout=5.0))
    await asyncio.sleep(0)
    assert not drain.done()  # blocked: debt not yet consumed

    res = await seq.post_for_block(EMIT_COMPLETION_TOOL, "hc")
    assert res == "debt_consumed"
    assert await asyncio.wait_for(drain, timeout=1.0) is True


async def test_await_completion_drain_times_out_returns_false():
    """F3: an un-consumed debt (no emit_completion block ever arrives) times out
    → False (the caller WARNs and proceeds)."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    _register_completion_debt(seq)
    assert await seq.await_completion_drain(
        "emit_completion:eng-1", timeout=0.05) is False


async def test_await_completion_drain_unknown_intent_returns_true():
    """F3: no debt registered (a cancel/error finalize) ⇒ drain returns True
    immediately."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    assert await seq.await_completion_drain("nope", timeout=0.05) is True
