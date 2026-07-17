"""Tests for the D5 anchor-scoped NARRATION BUFFER (Task C2) + the atomic
``OutputSequencer.post_unless_anchor_open`` read-decide-write (Sol r4-2), built
on top of Task C1's driver-injected ``open_anchor_state`` seam +
anchor-candidate + arming bookkeeping (design round-4 §D5).

Task C2 turns arming from a behavior-neutral latch (C1) into the real
buffer state machine:

* once a turn has SUCCESSFULLY surfaced a free-text ANCHOR, subsequent prose is
  BUFFERED (routed through the atomic op), never posted;
* a later tool_use FLUSHES the buffer (post-tool prose is legitimate) AND
  DISARMS suppression for the rest of the turn (r23-2);
* an answer arriving before ``result`` (the seam re-read reports no open anchor)
  FLUSHES the buffer;
* ``result`` with the anchor STILL open-and-unanswered DISCARDS the buffer (the
  F-LEAK2 kill).

The ``hold_pending`` persistence / crash-replay machinery is Task C3 — this task
keeps the buffer purely IN-MEMORY (held frames carry their frame coordinates so
C3 can add the checkpoint exemption on top without restructuring).

These tests drive a REAL relay over a REAL ``OutputSequencer`` (constructed with
tiny, injected-clock hold/timeout windows so nothing waits on real wall time —
never a mock of the sequencer, matching ``test_topic_stream.py``'s style)
against a REAL temp NDJSON log dir. Clocks are injected (``_now``/``_sleep``);
we never patch ``asyncio.sleep`` (the module-local / injected-clock rule,
CLAUDE.md memory cage).
"""
from __future__ import annotations

import asyncio

import pytest

from channels.output_sequencer import ASK_TOOL, OutputSequencer, projection_hash
from test_topic_stream import (
    Recorder,
    _init,
    _make_relay,
    _result,
    _text,
    _tool_in,
    _write_current,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _fast_sequencer(
    rec, *, slot_hold_s: float = 0.05, intent_timeout_s: float = 5.0,
    hold_poll_s: float = 0.05,
):
    """A REAL ``OutputSequencer`` with a fake clock/sleep so the 2s ordered-
    slot hold (and any intent-timeout math) resolves instantly in test time —
    the sleep fake ADVANCES the fake clock rather than actually waiting, so
    ``post_for_block``'s hold loop still exercises its real deadline logic."""
    clock = {"t": 0.0}

    async def _tick_sleep(dt: float) -> None:
        clock["t"] += dt

    seq = OutputSequencer(
        engagement_id="eng-1", topic_id=42,
        send_message=rec.send, edit_message=rec.edit,
        _now=lambda: clock["t"], _sleep=_tick_sleep,
        slot_hold_s=slot_hold_s, intent_timeout_s=intent_timeout_s,
        hold_poll_s=hold_poll_s,
    )
    return seq, clock


def _append_current(log_dir, frames) -> None:
    """Append NDJSON *frames* to ``<log_dir>/current`` (models a live poll)."""
    import json
    import os

    path = os.path.join(str(log_dir), "current")
    with open(path, "ab") as fh:
        for fr in frames:
            fh.write(json.dumps(fr).encode("utf-8") + b"\n")


def _anchor_ask(question: str = "Q?") -> dict:
    """A free-text-anchor ask tool_use block — NO ``options`` key (§D5 scope:
    free-text anchors only, never a button ask)."""
    return _tool_in(ASK_TOOL, {"question": question})


def _button_ask(question: str = "Pick one") -> dict:
    return _tool_in(ASK_TOOL, {"question": question, "options": ["a", "b"]})


def _narration_sends(rec, *, exclude=()):
    return [t for _tp, t in rec.sends if t not in exclude]


# ===========================================================================
# RED anchor (d): the atomic sequencer op (Sol r4-2) — deterministic interleave.
# ===========================================================================


async def test_atomic_op_holds_when_anchor_open():
    """A bare seam re-read races the late poster; the atomic op makes the
    read + narration write share the ONE writer lock the late poster uses.
    Deterministic interleave: the late poster takes the lock and posts the
    anchor BEFORE the atomic op acquires it, so the op observes the anchor open
    and HOLDS — narration never lands below the anchor."""
    rec = Recorder()
    seq, _clock = _fast_sequencer(rec)
    anchor = {"posted": False}
    seam = lambda: (1, 111) if anchor["posted"] else None  # noqa: E731
    holding = asyncio.Event()
    proceed = asyncio.Event()

    async def late_anchor_poster():
        async with seq.serialized():
            holding.set()
            await proceed.wait()          # hold the lock across the barrier
            mid = await rec.send(42, "[ANCHOR]")
            anchor["posted"] = True
            seq._high_water = mid

    async def narration():
        await holding.wait()              # the anchor task owns the lock
        proceed.set()                     # let it post the anchor + release
        return await seq.post_unless_anchor_open(
            "trailing", seam, poster=lambda: rec.send(42, "trailing"),
        )

    tp = asyncio.create_task(late_anchor_poster())
    status = await narration()
    await tp

    assert status == "held"
    assert _narration_sends(rec) == ["[ANCHOR]"]  # narration never below anchor


async def test_atomic_op_posts_when_anchor_absent():
    """No open anchor at the seam read ⇒ the atomic op runs the poster under the
    lock and returns ``"posted"``."""
    rec = Recorder()
    seq, _clock = _fast_sequencer(rec)
    status = await seq.post_unless_anchor_open(
        "narration", lambda: None, poster=lambda: rec.send(42, "narration"),
    )
    assert status == "posted"
    assert _narration_sends(rec) == ["narration"]


async def test_atomic_op_default_poster_opens_narration():
    """Called without an explicit poster, the atomic op posts *text* as a fresh
    narration message (belt-and-suspenders default)."""
    rec = Recorder()
    seq, _clock = _fast_sequencer(rec)
    status = await seq.post_unless_anchor_open("hello", lambda: None)
    assert status == "posted"
    assert _narration_sends(rec) == ["hello"]
    assert seq.narration_msg_id is not None


# ===========================================================================
# RED anchor (a): the live-observed F-LEAK2 sign-off — DISCARD at result.
# ===========================================================================


async def test_anchor_open_at_result_discards_trailing_narration(tmp_path):
    """The exact live sequence: an anchor surfaced, the agent narrated the
    observed sign-off, and ``result`` arrived with the anchor STILL open-and-
    unanswered ⇒ the trailing narration is DISCARDED — ZERO narration posts."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    signoff = (
        "The ask posted as an open-ended anchor. I'll end my turn and wait for "
        "the operator's answer before proceeding."
    )
    _write_current(tmp_path, [_init(), _anchor_ask(), _text(signoff), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500),  # open-and-unanswered throughout
    )
    await relay.run()

    assert rec.sends == []   # the sign-off never reached the wire (F-LEAK2 kill)
    assert rec.edits == []


# ===========================================================================
# RED anchor (b): tool_use FLUSHES + DISARMS (Sol r23-2) — both segments post.
# ===========================================================================


async def test_tool_use_flushes_buffer_and_disarms(tmp_path):
    """anchor → prose → tool_use → post-tool prose → ``result`` (anchor still
    open): the tool_use FLUSHES the buffered prose (post-tool work is
    legitimate) and DISARMS, so the post-tool prose also posts — BOTH segments
    are visible even though the anchor is open at result."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    _write_current(tmp_path, [
        _init(), _anchor_ask(),
        _text("alpha "),
        _tool_in("Bash", {"command": "ls"}),
        _text("beta"),
        _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500),  # anchor stays open all turn
    )
    await relay.run()

    assert _narration_sends(rec) == ["alpha "]   # flushed on the tool_use
    assert rec.edits[-1][2] == "alpha beta"       # post-tool prose appended
    # DISARMED: the post-tool prose is not re-buffered/discarded at result.


async def test_second_anchor_reamms_after_flush(tmp_path):
    """Re-arming happens ONLY when a NEW anchor surfaces. A first anchor's
    buffer flushes+disarms on a tool_use; a SECOND anchor block re-arms, so its
    trailing prose is buffered and DISCARDED at result again."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    _write_current(tmp_path, [
        _init(), _anchor_ask("Q1?"),
        _text("first "),
        _tool_in("Bash", {"command": "ls"}),   # flush "first " + disarm
        _anchor_ask("Q2?"),                     # a NEW anchor surfaces → re-arm
        _text("second signoff"),                # buffered under the new anchor
        _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500),
    )
    await relay.run()

    assert _narration_sends(rec) == ["first "]   # only the flushed segment
    # "second signoff" was buffered under Q2 and DISCARDED at result.


# ===========================================================================
# RED anchor (c): an answer before result FLUSHES the buffer.
# ===========================================================================


async def test_answer_before_result_flushes_buffer(tmp_path):
    """anchor → prose → answer arrives → ``result``: the seam re-read at result
    reports no open anchor (answered), so the buffered prose is FLUSHED (an
    answer legitimizes the trailing prose)."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    seam_state = {"open": (9, 900)}
    seam = lambda: seam_state["open"]  # noqa: E731

    _write_current(tmp_path, [_init(), _anchor_ask(), _text("bye")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq, open_anchor_state=seam,
    )
    await relay.run()

    assert relay._suppressing_for == (9, 900)   # armed mid-turn
    assert rec.sends == []                        # buffered, not yet posted

    # The operator ANSWERS before the turn's result — the seam now reports the
    # anchor closed.
    seam_state["open"] = None
    _append_current(tmp_path, [_result()])
    await relay.run()

    assert _narration_sends(rec) == ["bye"]      # FLUSHED at result


# ===========================================================================
# C1 bookkeeping, updated for C2 behaviour.
# ===========================================================================


async def test_late_post_arms_then_discards(tmp_path):
    """(C1 (a), now with the C2 consumer) The ask block resolves
    ``slot_timeout`` (nothing registered — a late out-of-band post); suppression
    does NOT arm at block-resolution time (seam still None), then arms once the
    seam reports the anchor open before the next text frame. The trailing prose
    is buffered and DISCARDED at result (anchor still open)."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)

    seam_state = {"open": None}
    seam = lambda: seam_state["open"]  # noqa: E731

    _write_current(tmp_path, [_init(), _anchor_ask()])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq, open_anchor_state=seam,
    )
    await relay.run()

    assert relay._anchor_candidate is not None
    assert relay._suppressing_for is None  # seam None at block time ⇒ unarmed

    # The anchor surfaces OUT OF BAND (a late watcher post) between polls.
    seam_state["open"] = (7, 555)
    _append_current(tmp_path, [_text("hello")])
    await relay.run()

    assert relay._suppressing_for == (7, 555)  # armed before the text frame
    assert rec.sends == []                      # "hello" buffered, not posted

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert rec.sends == []                       # DISCARDED (anchor still open)


async def test_post_first_debt_consumed_arms_then_discards(tmp_path):
    """(C1 (b)) The intent times out and posts OUT OF BAND before the relay
    reaches its block; the block resolves ``debt_consumed``. Still records a
    candidate and arms once the seam confirms open — trailing prose is buffered
    and DISCARDED at result (anchor still open)."""
    rec, events = Recorder(), []
    seq, clock = _fast_sequencer(rec)

    h = projection_hash(ASK_TOOL, {"question": "Q?"})

    async def poster():
        return await rec.send(42, "[anchor]Q?")

    seq.register_intent(
        request_id="a1", tool_name=ASK_TOOL, projection_hash=h, poster=poster,
    )
    seq.arm_intent("a1")
    clock["t"] = 10.0  # past intent_timeout_s (5.0)
    await seq.process_intents_once()  # posts out-of-band NOW; leaves the debt

    seam = lambda: (3, 777)  # noqa: E731 — the anchor is genuinely open

    _write_current(tmp_path, [_init(), _anchor_ask(), _text("after")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq, open_anchor_state=seam,
    )
    await relay.run()

    assert relay._anchor_candidate is not None
    assert relay._suppressing_for == (3, 777)
    assert _narration_sends(rec, exclude=("[anchor]Q?",)) == []  # buffered

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert _narration_sends(rec, exclude=("[anchor]Q?",)) == []  # DISCARDED


async def test_failed_poster_never_arms_narration_posts(tmp_path):
    """(C1 (c)) ``post_for_block`` can return ``"posted"`` even though the
    ARMED intent's poster recorded a FAILURE outcome — the driver's ledger
    never gained the entry, so the seam never reports it open. The candidate is
    recorded, suppression never arms, and narration is routed through the atomic
    op which POSTS it (the anchor is genuinely absent)."""
    async def failing_poster():
        return None  # simulates a wire post failure

    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    h = projection_hash(ASK_TOOL, {"question": "Q?"})
    seq.register_intent(
        request_id="f1", tool_name=ASK_TOOL, projection_hash=h,
        poster=failing_poster,
    )
    seq.arm_intent("f1")

    _write_current(tmp_path, [_init(), _anchor_ask(), _text("after")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: None,  # the ledger never gained the entry
    )
    await relay.run()

    assert relay._anchor_candidate is not None  # status was "posted" (misleading)
    assert relay._suppressing_for is None        # seam never confirmed it open
    assert _narration_sends(rec) == ["after"]    # posted via the atomic op

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert _narration_sends(rec) == ["after"]    # unchanged after the turn closes


async def test_default_open_anchor_state_is_inert(tmp_path):
    """An un-injected ``open_anchor_state`` (default ``None``) leaves the
    feature completely inert — a matching ask block still records a candidate
    (harmless bookkeeping) but suppression can never arm and narration posts
    unbuffered. Backward-compatible for every other ``TopicStreamRelay``
    caller."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    _write_current(tmp_path, [_init(), _anchor_ask(), _text("hi")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events, sequencer=seq)  # no seam
    await relay.run()

    assert relay._anchor_candidate is not None
    assert relay._suppressing_for is None
    assert _narration_sends(rec) == ["hi"]

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert _narration_sends(rec) == ["hi"]


async def test_turn_boundary_resets_candidate_arming_and_buffer(tmp_path):
    """An anchor armed mid-turn (with a non-empty buffer) must not leak into the
    NEXT turn — the turn-ending ``result`` frame resets candidate, arming AND
    the buffer exactly like every other per-turn field."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    _write_current(tmp_path, [_init(), _anchor_ask(), _text("hi")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (1, 100),
    )
    await relay.run()
    assert relay._suppressing_for == (1, 100)  # armed mid-turn (before result)
    assert relay._anchor_buffer                # "hi" buffered

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert relay._anchor_candidate is None
    assert relay._suppressing_for is None
    assert relay._anchor_buffer == []          # the turn boundary cleared it


async def test_button_ask_with_options_is_not_a_candidate(tmp_path):
    """Scope (§D5): "not button asks (they block the turn anyway)" — a
    button-style ask (``options`` given) never sets ``_anchor_candidate``, so
    suppression never arms and its trailing prose posts unbuffered."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    _write_current(tmp_path, [_init(), _button_ask(), _text("hi"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (1, 100),
    )
    await relay.run()

    assert relay._anchor_candidate is None
    assert relay._suppressing_for is None
    assert _narration_sends(rec) == ["hi"]     # posted (never buffered)
