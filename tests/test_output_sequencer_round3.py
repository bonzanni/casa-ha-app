"""Tests for the v0.83.0 (round 3, A9) OUTPUT SEQUENCER discrete primitives:
``post_discrete`` / ``edit_discrete`` (markup-capable single-writer writes) and
the A3(c) ``mark_intent_compensated`` path.

Real ``OutputSequencer`` + injected clock (``_now``/``_sleep``) — per the
memory-cage rule we NEVER patch ``<module>.asyncio.sleep``. The FAILED-edit
end-to-end test drives the REAL ``settle_gate.confirmed_settle_edit`` (the exact
gate every settle path funnels through) to prove the ``bool`` contract.
"""
from __future__ import annotations

import asyncio

from channels.output_sequencer import (
    MARKUP_ABSENT,
    OutputSequencer,
    _ABSENT,
    _markup_tristate,
)
from settle_gate import confirmed_settle_edit

# Distinct opaque keyboard objects — the sequencer only ever serializes them via
# ``_markup_tristate`` (repr), never inspects their shape.
KBD1 = ("keyboard", 1)
KBD2 = ("keyboard", 2)


class Ids:
    """Shared monotonic id source so plain (narration) and markup (discrete)
    sends draw from ONE increasing id space — mirrors Telegram's globally
    increasing message ids, so a later post always has a higher id."""

    def __init__(self, start: int = 500) -> None:
        self.n = start

    def next(self) -> int:
        self.n += 1
        return self.n


class PlainRecorder:
    """Plain text send/edit wire (narration / summary paths)."""

    def __init__(self, ids: Ids) -> None:
        self.ids = ids
        self.sends: list[tuple[int, str, int | None]] = []
        self.edits: list[tuple[int, int, str]] = []

    async def send(self, topic_id: int, text: str, reply_to: int | None = None):
        self.sends.append((topic_id, text, reply_to))
        return self.ids.next()

    async def edit(self, topic_id: int, message_id: int, text: str) -> bool:
        self.edits.append((topic_id, message_id, text))
        return True


class MarkupRecorder:
    """Markup-capable send/edit wire (post_discrete / edit_discrete)."""

    def __init__(self, ids: Ids) -> None:
        self.ids = ids
        self.sends: list[tuple[int, str, object, int | None]] = []
        self.edits: list[tuple[int, int, object, object]] = []
        self.send_returns_none = False
        self.edit_fails = 0  # number of leading edits that return False

    async def send(self, topic_id: int, text: str, markup, reply_to=None):
        self.sends.append((topic_id, text, markup, reply_to))
        if self.send_returns_none:
            return None
        return self.ids.next()

    async def edit(self, topic_id: int, message_id: int, text, markup) -> bool:
        if self.edit_fails > 0:
            self.edit_fails -= 1
            return False
        self.edits.append((topic_id, message_id, text, markup))
        return True


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, dt: float) -> None:
        self.t += dt


def _make_seq(ids: Ids | None = None):
    ids = ids or Ids()
    plain = PlainRecorder(ids)
    markup = MarkupRecorder(ids)
    clock = Clock()
    seq = OutputSequencer(
        engagement_id="eng-r3",
        topic_id=42,
        send_message=plain.send,
        edit_message=plain.edit,
        send_message_markup=markup.send,
        edit_message_markup=markup.edit,
        _now=clock.now,
        _sleep=clock.sleep,
        slot_hold_s=2.0,
        intent_timeout_s=10.0,
        hold_poll_s=0.05,
    )
    return seq, plain, markup, clock, ids


# ---------------------------------------------------------------------------
# post_discrete
# ---------------------------------------------------------------------------


async def test_post_discrete_seals_narration_advances_high_water_seeds_cache():
    seq, plain, markup, _clock, _ids = _make_seq()
    nid = await seq.open_narration("working...")
    assert seq.narration_msg_id == nid
    assert seq.high_water == nid

    mid = await seq.post_discrete("decision?", markup=KBD1)
    assert mid is not None
    # narration SEALED
    assert seq.narration_msg_id is None
    # high-water advanced to the discrete mid (higher, shared id space)
    assert seq.high_water == mid
    assert mid > nid
    # wire got the markup-capable send
    assert markup.sends == [(42, "decision?", KBD1, None)]
    # cache seeded tri-state
    assert seq._edit_cache[mid] == ("decision?", _markup_tristate(KBD1))


async def test_post_discrete_reply_to_passthrough():
    seq, _plain, markup, _clock, _ids = _make_seq()
    mid = await seq.post_discrete("q", markup=KBD1, reply_to=7)
    assert mid is not None
    assert markup.sends[-1] == (42, "q", KBD1, 7)


async def test_post_discrete_revalidate_declined_returns_none_no_send_no_state():
    seq, plain, markup, _clock, _ids = _make_seq()
    nid = await seq.open_narration("working...")
    hw_before = seq.high_water

    async def _decline():
        return False

    mid = await seq.post_discrete("q", markup=KBD1, revalidate=_decline)
    assert mid is None
    assert markup.sends == []            # zero wire sends
    assert seq.narration_msg_id == nid   # narration NOT sealed
    assert seq.high_water == hw_before   # state unchanged

    # sync revalidate also honored
    mid2 = await seq.post_discrete("q", markup=KBD1, revalidate=lambda: False)
    assert mid2 is None
    assert markup.sends == []


async def test_post_discrete_revalidate_accepted_sends():
    seq, _plain, markup, _clock, _ids = _make_seq()
    mid = await seq.post_discrete("q", markup=KBD1, revalidate=lambda: True)
    assert mid is not None
    assert len(markup.sends) == 1


# ---------------------------------------------------------------------------
# edit_discrete
# ---------------------------------------------------------------------------


async def test_edit_discrete_markup_only_edit_touches_no_narration_state():
    seq, _plain, markup, _clock, _ids = _make_seq()
    mid = await seq.post_discrete("body", markup=KBD1)
    nar_before, hw_before = seq.narration_msg_id, seq.high_water

    ok = await seq.edit_discrete(mid, markup=KBD2)  # text=None → markup-only
    assert ok is True
    assert markup.edits == [(42, mid, None, KBD2)]
    # NEVER touches narration / high-water
    assert seq.narration_msg_id == nar_before
    assert seq.high_water == hw_before


async def test_edit_discrete_noop_cache_skip():
    seq, _plain, markup, _clock, _ids = _make_seq()
    mid = await seq.post_discrete("body", markup=KBD1)
    # identical (text, markup) → no-op skip, ZERO wire edits
    ok = await seq.edit_discrete(mid, text="body", markup=KBD1)
    assert ok is True
    assert markup.edits == []


async def test_edit_discrete_failed_wire_survives_confirmed_settle_edit():
    seq, _plain, markup, clock, _ids = _make_seq()
    mid = await seq.post_discrete("body", markup=KBD1)
    markup.edit_fails = 99  # every wire edit fails

    # A caller's ledger record; a CONFIRMED settle would delete it.
    ledger = {"open": True}

    async def _do_edit() -> bool:
        return await seq.edit_discrete(mid, text="answered", markup=KBD2)

    confirmed = await confirmed_settle_edit(_do_edit, sleep=clock.sleep)
    assert confirmed is False          # bool contract, not truthy "failed"
    if confirmed:
        ledger.pop("open")
    assert ledger == {"open": True}    # recovery record survives


async def test_edit_discrete_revalidate_declined_returns_false_no_edit():
    seq, _plain, markup, _clock, _ids = _make_seq()
    mid = await seq.post_discrete("body", markup=KBD1)
    ok = await seq.edit_discrete(mid, text="x", markup=KBD2, revalidate=lambda: False)
    assert ok is False
    assert markup.edits == []


# ---------------------------------------------------------------------------
# bounded discrete-cache FIFO (cap 64)
# ---------------------------------------------------------------------------


async def test_discrete_cache_fifo_evicts_oldest_narration_untouched():
    seq, _plain, markup, _clock, _ids = _make_seq()
    # a NON-discrete (summary) cache entry — must never be evicted by the FIFO
    await seq.edit_summary(9999, "summary line")
    assert 9999 in seq._edit_cache

    mids: list[int] = []
    for i in range(65):  # cap 64 → the first (oldest) discrete entry is evicted
        m = await seq.post_discrete(f"d{i}", markup=KBD1)
        mids.append(m)

    oldest = mids[0]
    assert oldest not in seq._edit_cache      # evicted
    assert mids[-1] in seq._edit_cache        # newest retained
    assert 9999 in seq._edit_cache            # summary untouched

    # re-edit of the evicted mid works — the no-op gate simply re-edits once
    before = len(markup.edits)
    ok = await seq.edit_discrete(oldest, text="reopen", markup=KBD2)
    assert ok is True
    assert len(markup.edits) == before + 1


# ---------------------------------------------------------------------------
# mark_intent_compensated (A3(c) compensated-intent path)
# ---------------------------------------------------------------------------


async def test_mark_intent_compensated_invariants():
    seq, _plain, _markup, _clock, ids = _make_seq()
    seq.register_intent(
        request_id="anc-1", tool_name="tool", projection_hash="h", poster="x",
    )
    delivered = ids.next()
    intent = await seq.mark_intent_compensated("anc-1", delivered)

    # high-water advanced to the delivered mid
    assert seq.high_water == delivered
    # exactly-once compensated outcome
    assert intent.outcome == {
        "ok": False, "message_id": delivered, "compensated": True,
    }
    # post_failed NOT re-fired; retired from matching
    assert intent.post_failed is False
    assert intent.consumed is True
    assert intent.matchable() is False

    # await resolution returns the recorded outcome
    outcome = await seq.await_intent_resolution("anc-1", timeout=1.0)
    assert outcome == {"ok": False, "message_id": delivered, "compensated": True}

    # idempotent: a repeat call does not change the outcome or double-resolve
    again = await seq.mark_intent_compensated("anc-1", delivered)
    assert again.outcome == {
        "ok": False, "message_id": delivered, "compensated": True,
    }
    assert seq.high_water == delivered


async def test_mark_intent_compensated_unknown_request_returns_none():
    seq, *_ = _make_seq()
    assert await seq.mark_intent_compensated("nope", 123) is None


async def test_later_ask_opens_below_compensated_orphan():
    seq, plain, markup, _clock, ids = _make_seq()
    seq.register_intent(
        request_id="anc-1", tool_name="tool", projection_hash="h", poster="x",
    )
    orphan_mid = ids.next()
    await seq.mark_intent_compensated("anc-1", orphan_mid)
    assert seq.high_water == orphan_mid

    # a later ask/narration opens BELOW the orphan (higher logical position)
    later = await seq.open_narration("a later ask")
    assert later > orphan_mid
    assert seq.high_water == later
    # recorded send order: the narration send lands after the compensated mid
    assert plain.sends[-1] == (42, "a later ask", None)


async def test_later_post_discrete_opens_below_compensated_orphan():
    seq, _plain, markup, _clock, ids = _make_seq()
    seq.register_intent(
        request_id="anc-1", tool_name="tool", projection_hash="h", poster="x",
    )
    orphan_mid = ids.next()
    await seq.mark_intent_compensated("anc-1", orphan_mid)

    later = await seq.post_discrete("later ask", markup=KBD1)
    assert later > orphan_mid
    assert seq.high_water == later


# ---------------------------------------------------------------------------
# belt-and-suspenders: no markup wire injected → clear RuntimeError
# ---------------------------------------------------------------------------


async def test_discrete_primitives_require_markup_wire():
    seq = OutputSequencer(
        engagement_id="eng-x", topic_id=1,
        send_message=PlainRecorder(Ids()).send,
        edit_message=PlainRecorder(Ids()).edit,
    )
    import pytest

    with pytest.raises(RuntimeError):
        await seq.post_discrete("q", markup=KBD1)
    with pytest.raises(RuntimeError):
        await seq.edit_discrete(5, markup=KBD1)


def test_markup_tristate_absent_sentinel():
    assert _markup_tristate(_ABSENT) == MARKUP_ABSENT
