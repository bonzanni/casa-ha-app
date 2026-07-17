"""Tests for the D5 anchor-candidate + driver-injected ``open_anchor_state``
seam in ``drivers.topic_stream`` (design round-4 §D5 "Successful-anchor
identity comes from the DRIVER" / "Arming survives out-of-band posting",
Task C1).

Task C1 builds ONLY the seam + per-turn anchor-candidate + ARM bookkeeping —
there is no buffering/flush/discard consumer yet (Task C2) and no
``hold_pending`` marker (Task C3). These tests therefore drive a REAL relay
over a REAL ``OutputSequencer`` (constructed with tiny, injected-clock hold/
timeout windows so nothing waits on real wall time — never a mock of the
sequencer, matching ``test_topic_stream.py``'s style) against a REAL temp
NDJSON log dir, and assert two things: (1) the candidate + arm bookkeeping
itself, and (2) that arming is completely BEHAVIOR-NEUTRAL for narration
output in this task — nothing is buffered or discarded yet. Clocks are
injected (``_now``/``_sleep``); we never patch ``asyncio.sleep`` (the
module-local / injected-clock rule, CLAUDE.md memory cage).
"""
from __future__ import annotations

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


# ---------------------------------------------------------------------------
# (a) block-first / late-post: seam None at block time, open later.
# ---------------------------------------------------------------------------


async def test_late_post_arms_before_next_text_frame(tmp_path):
    """The ask block resolves ``slot_timeout`` (nothing registered — the
    watcher posts it out-of-band LATER). Suppression must NOT arm at block-
    resolution time (the seam is still None then); it arms once the seam
    reports the anchor open, checked before the NEXT text frame."""
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

    # Candidate recorded (slot_timeout), but the seam is still None ⇒ unarmed.
    assert relay._anchor_candidate is not None
    assert relay._suppressing_for is None

    # The anchor surfaces OUT OF BAND (a late watcher post) between polls.
    seam_state["open"] = (7, 555)
    _append_current(tmp_path, [_text("hello")])
    await relay.run()

    assert relay._suppressing_for == (7, 555)

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert [t for _tp, t in rec.sends] == ["hello"]  # narration unaffected (C1 neutral)


# ---------------------------------------------------------------------------
# (b) post-first / debt_consumed: the post preceded the block.
# ---------------------------------------------------------------------------


async def test_post_first_debt_consumed_arms(tmp_path):
    """The intent times out and posts OUT OF BAND *before* the relay ever
    reaches its tool_use block; the block then resolves ``debt_consumed``
    (§D5 "Arming survives out-of-band posting" — the post PRECEDED the
    block). Still records a candidate and arms once the seam confirms open."""
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

    seam = lambda: (3, 777)  # noqa: E731 — the anchor is already genuinely open

    # Checked BEFORE ``result`` — the turn-ending frame resets candidate/arm
    # state (test (e)), so the armed assertion must land while the turn is
    # still open.
    _write_current(tmp_path, [_init(), _anchor_ask(), _text("after")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq, open_anchor_state=seam,
    )
    await relay.run()

    assert relay._anchor_candidate is not None
    assert relay._suppressing_for == (3, 777)

    _append_current(tmp_path, [_result()])
    await relay.run()
    narration = [t for _tp, t in rec.sends if t != "[anchor]Q?"]
    assert narration == ["after"]  # narration unaffected (C1 neutral)


# ---------------------------------------------------------------------------
# (c) a FAILED poster outcome (seam stays None) never arms.
# ---------------------------------------------------------------------------


async def test_failed_poster_outcome_never_arms(tmp_path):
    """``post_for_block`` can return ``"posted"`` even though the ARMED
    intent's own poster recorded a FAILURE outcome (§D5 "Successful-anchor
    identity comes from the DRIVER, not the sequencer's block status") — the
    driver's ledger never gained the entry, so the seam never reports it
    open. The candidate is still recorded, but suppression never arms."""
    async def failing_poster():
        return None  # simulates a wire post failure (post_failed=True)

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

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert [t for _tp, t in rec.sends] == ["after"]  # narration unaffected


# ---------------------------------------------------------------------------
# (d) arming is behavior-neutral in C1 — narration still posts normally.
# ---------------------------------------------------------------------------


async def test_arming_is_behavior_neutral_narration_unaffected(tmp_path):
    """Task C1 builds ONLY the seam + candidate + arm boolean — no buffering
    consumer exists yet, so narration posts EXACTLY as it would pre-C1 even
    once armed (spec: "arming with no buffering behavior behind it yet is
    fine and must be behavior-neutral for narration output in this task")."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    _write_current(tmp_path, [
        _init(), _anchor_ask(),
        _text("one "), _text("two "), _text("three"),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (1, 100),
    )
    await relay.run()

    assert relay._suppressing_for == (1, 100)  # armed
    # Every narration fragment landed, in order, unbuffered/undiscarded.
    assert rec.sends[0][1] == "one "
    assert rec.edits[-1][2] == "one two three"

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert rec.sends[0][1] == "one "  # still unaffected after the turn closes
    assert [t for _tp, _mid, t in rec.edits][-1] == "one two three"


async def test_default_open_anchor_state_is_inert(tmp_path):
    """An un-injected ``open_anchor_state`` (default ``None``) leaves this
    feature completely inert — a matching ask block still records a
    candidate (harmless bookkeeping) but suppression can never arm, and
    narration is entirely unaffected. Backward-compatible for every other
    ``TopicStreamRelay`` caller that has not wired the seam yet."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    _write_current(tmp_path, [_init(), _anchor_ask(), _text("hi")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events, sequencer=seq)  # no seam
    await relay.run()

    assert relay._anchor_candidate is not None
    assert relay._suppressing_for is None

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert [t for _tp, t in rec.sends] == ["hi"]


# ---------------------------------------------------------------------------
# (e) turn boundaries reset candidate/arming state.
# ---------------------------------------------------------------------------


async def test_turn_boundary_resets_candidate_and_arming(tmp_path):
    """An anchor armed mid-turn (before ``result``) must not leak into the
    NEXT turn's in-memory state — the turn-ending ``result`` frame resets
    candidate + arming exactly like every other per-turn field."""
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

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert relay._anchor_candidate is None
    assert relay._suppressing_for is None  # the turn boundary reset it


# ---------------------------------------------------------------------------
# Scope: free-text anchors ONLY (§D5) — a button ask is never a candidate.
# ---------------------------------------------------------------------------


async def test_button_ask_with_options_is_not_a_candidate(tmp_path):
    """Scope (§D5): "not button asks (they block the turn anyway)" — a
    button-style ask (``options`` given) never sets ``_anchor_candidate``,
    even if its block resolves the same way a free-text anchor's would."""
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
