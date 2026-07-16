"""Task 8 / v0.83.0 §A3(a)+(c) — ingress reservation, reply/stacking gates,
allocation-failure refusal, initial-anchor add-failure compensation
(F-ORDER / F-NOWAIT structural gates).

Exercises the WHOLE contract with a REAL ``ClaudeCodeDriver`` + REAL
``OutputSequencer`` + REAL ``VerdictBroker`` + REAL ``EngagementRegistry`` over a
tmp tombstone. The ask/reply handlers are the real ``_make_channel_handlers``
factory output; the relay-mediated deferred poster is driven deterministically
by ``seq.post_for_block``. Injected/monkeypatched clocks only; never patches
``<module>.asyncio.sleep`` (the shared-attribute memory-cage rule).

Gates under test:
* (a) live-pending REPLY gate — refuse a ``reply`` while a question is live;
* (c) ask-ingress STACKING gate — an atomic per-engagement reservation
  (``ask_inflight`` marker under the ask-maintenance lock) so exactly one of two
  concurrent asks wins; the loser gets ``question_pending``;
* number-allocation failure is terminal BEFORE any wire post (absent-vs-raising
  allocator distinction);
* initial-anchor add-failure compensation (withdraw edit + compensated intent).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker
from channels.output_sequencer import ASK_TOOL, OutputSequencer

pytestmark = pytest.mark.asyncio

_ANCHOR_HASH = "anchor-hash"
_BTN_HASH = "btn-hash"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Chan:
    """Telegram fake: keyboard/anchor/reply posts AND the sequencer narration
    posts all draw from ONE monotonic id counter, so post order == id order.
    ``edit_topic_message`` records every settle/withdraw edit; failures are
    injectable per-primitive for the delivery-failed / add-failure paths."""

    def __init__(self) -> None:
        self._next = 100
        self.keyboards: list[tuple[int, str]] = []
        self.replies: list[tuple[int, str]] = []
        self.anchors: list[tuple[int, str]] = []
        self.narrations: list[tuple[int, str]] = []
        self.edits: list[dict] = []
        self.send_returns_none = False

    def _id(self) -> int:
        m = self._next
        self._next += 1
        return m

    async def post_options_keyboard(
        self, *, engagement_id, request_id, question, options,
    ) -> int:
        m = self._id()
        self.keyboards.append((m, question))
        return m

    async def send_response_to_topic(self, topic_id, text) -> int | None:
        if self.send_returns_none:
            return None
        m = self._id()
        self.replies.append((m, text))
        self.anchors.append((m, text))
        return m

    async def edit_topic_message(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        self.edits.append(
            {"message_id": message_id, "text": text,
             "clear_keyboard": clear_keyboard})
        return True

    # sequencer narration primitives (share the SAME counter)
    async def narration_send(self, topic_id, text, reply_to=None) -> int:
        m = self._id()
        self.narrations.append((m, text))
        return m

    async def narration_edit(self, topic_id, message_id, text) -> bool:
        return True


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def _body(resp: web.Response) -> dict:
    return json.loads(resp.text)


@pytest.fixture
def fresh_broker(monkeypatch):
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture
async def wired(tmp_path, fresh_broker, monkeypatch):
    from engagement_registry import EngagementRegistry
    from drivers.claude_code_driver import ClaudeCodeDriver
    from channels.channel_handlers import _make_channel_handlers
    from unittest.mock import AsyncMock

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    chan = _Chan()
    seq = OutputSequencer(
        engagement_id=rec.id, topic_id=42,
        send_message=chan.narration_send, edit_message=chan.narration_edit)
    drv = ClaudeCodeDriver(
        engagements_root=str(tmp_path / "eng"),
        send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        edit_topic_message=chan.edit_topic_message, registry=reg)
    drv._sequencers[rec.id] = seq
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", drv)
    handlers = _make_channel_handlers(
        telegram_channel=chan, engagement_registry=reg)
    return {
        "reg": reg, "rec": rec, "chan": chan, "seq": seq, "drv": drv,
        "broker": fresh_broker, "ask": handlers["/internal/channel/ask"],
        "send": handlers["/internal/channel/send_to_topic"],
    }


def _btn_payload(eid, rid="b1", *, hash=_BTN_HASH, **over):
    base = {
        "engagement_id": eid, "request_id": rid, "question": "Proceed?",
        "options": ["A", "B"], "timeout_s": 60, "projection_hash": hash,
    }
    base.update(over)
    return base


def _anchor_payload(eid, rid="a1", *, hash=_ANCHOR_HASH, **over):
    base = {
        "engagement_id": eid, "request_id": rid, "question": "DB name?",
        "options": [], "timeout_s": 60, "projection_hash": hash,
    }
    base.update(over)
    return base


async def _drive_button(wired, task, rid, *, hash=_BTN_HASH, option_index=0):
    """Drive a pending button ask to an ANSWERED resolution."""
    await asyncio.sleep(0.02)
    await wired["seq"].post_for_block(ASK_TOOL, hash)
    assert wired["broker"].deliver(
        namespace="engagement_ask", scope=wired["rec"].id, request_id=rid,
        option_index=option_index, actor_id=555) == "delivered"
    resp = await asyncio.wait_for(task, timeout=1.0)
    await wired["broker"].drain_hooks()
    return resp


async def _drive_anchor(wired, task, *, hash=_ANCHOR_HASH):
    """Drive a pending anchor ask through its relay-deferred post."""
    await asyncio.sleep(0.02)
    await wired["seq"].post_for_block(ASK_TOOL, hash)
    resp = await asyncio.wait_for(task, timeout=1.0)
    return resp


# ===========================================================================
# (a) live-pending REPLY gate — refuse / allow matrix
# ===========================================================================


class TestReplyGate:
    async def test_reply_refused_while_button_ask_pending(self, wired):
        eid = wired["rec"].id
        # A LIVE (unresolved) broker ask makes BROKER.pending non-empty.
        wired["broker"].register(
            namespace="engagement_ask", scope=eid, request_id="q1",
            timeout_s=60, meta={})
        resp = await wired["send"](_FakeRequest(
            {"engagement_id": eid, "text": "meanwhile..."}))
        body = _body(resp)
        assert body["ok"] is False
        assert body["error"] == "question_pending"
        assert not wired["chan"].replies  # nothing posted

    async def test_reply_refused_while_unanswered_anchor_open(self, wired):
        eid, reg = wired["rec"].id, wired["reg"]
        n = await reg.allocate_question_number(eid)
        await reg.add_open_question(eid, n, 7001, text="Q1: DB?", kind="anchor")
        resp = await wired["send"](_FakeRequest(
            {"engagement_id": eid, "text": "meanwhile..."}))
        assert _body(resp)["error"] == "question_pending"

    async def test_reply_refused_while_marker_set_no_gap(self, wired):
        """No GAP: a reply fired while the ingress marker is set (ask reserved but
        not yet durable) is refused, even with broker empty + no anchor yet."""
        eid = wired["rec"].id
        wired["drv"].set_ask_inflight(eid, "pending-q")
        resp = await wired["send"](_FakeRequest(
            {"engagement_id": eid, "text": "meanwhile..."}))
        assert _body(resp)["error"] == "question_pending"

    async def test_reply_allowed_after_tap_answer(self, wired):
        eid = wired["rec"].id
        req, _ = wired["broker"].register(
            namespace="engagement_ask", scope=eid, request_id="q1",
            timeout_s=60, meta={})
        # Tap answers it → broker scope empties.
        wired["broker"].deliver(
            namespace="engagement_ask", scope=eid, request_id="q1",
            option_index=0, actor_id=555)
        resp = await wired["send"](_FakeRequest(
            {"engagement_id": eid, "text": "carry on"}))
        assert _body(resp)["ok"] is True  # eager post (no projection_hash)
        assert wired["chan"].replies

    async def test_reply_allowed_after_expiry(self, wired):
        """An EXPIRED ask (no live broker request, no anchor) → reply allowed."""
        eid = wired["rec"].id
        resp = await wired["send"](_FakeRequest(
            {"engagement_id": eid, "text": "wrap up"}))
        assert _body(resp)["ok"] is True

    async def test_reply_allowed_for_answered_but_unconfirmed_anchor(self, wired):
        """Task 6 ``answered`` split: an anchor whose answer landed but whose
        settle edit is unconfirmed stops counting as live → reply allowed."""
        eid, reg = wired["rec"].id, wired["reg"]
        n = await reg.allocate_question_number(eid)
        await reg.add_open_question(eid, n, 7002, text="Q1: DB?", kind="anchor")
        await reg.mark_question_answered(eid, n)  # answered, entry still present
        resp = await wired["send"](_FakeRequest(
            {"engagement_id": eid, "text": "the answer is casa"}))
        assert _body(resp)["ok"] is True

    async def test_reply_allowed_for_reserved_anchor(self, wired):
        """Task 7 reservation also hides the anchor from the reply gate."""
        eid, reg = wired["rec"].id, wired["reg"]
        n = await reg.allocate_question_number(eid)
        await reg.add_open_question(eid, n, 7003, text="Q1: DB?", kind="anchor")
        assert wired["drv"].reserve_answer(eid) is not None
        resp = await wired["send"](_FakeRequest(
            {"engagement_id": eid, "text": "reserved answer"}))
        assert _body(resp)["ok"] is True


# ===========================================================================
# (c) ask-ingress STACKING gate — exactly one wins
# ===========================================================================


class TestStackingGate:
    async def test_concurrent_button_button_one_wins(self, wired):
        eid = wired["rec"].id
        t1 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid, "ba"))))
        t2 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid, "bb"))))
        done, pending = await asyncio.wait(
            {t1, t2}, timeout=0.3, return_when=asyncio.ALL_COMPLETED)
        # Exactly one refused (fast), one still awaiting the tap.
        assert len(done) == 1 and len(pending) == 1
        loser = done.pop()
        assert _body(loser.result())["error"] == "question_pending"
        # The winner is the sole live broker request.
        live = wired["broker"].pending(namespace="engagement_ask", scope=eid)
        assert len(live) == 1
        winner_rid = live[0]
        # A same-request_id reattach of the WINNER still passes (no refusal).
        t3 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid, winner_rid))))
        await asyncio.sleep(0.02)
        # Drive the winner's relay-deferred keyboard post, then tap.
        await wired["seq"].post_for_block(ASK_TOOL, _BTN_HASH)
        wired["broker"].deliver(
            namespace="engagement_ask", scope=eid, request_id=winner_rid,
            option_index=0, actor_id=555)
        winner = pending.pop()
        r_win = await asyncio.wait_for(winner, timeout=1.0)
        r_re = await asyncio.wait_for(t3, timeout=1.0)
        await wired["broker"].drain_hooks()
        assert _body(r_win)["outcome"] == "answered"
        assert _body(r_re)["outcome"] == "answered"
        assert len(wired["chan"].keyboards) == 1  # exactly one keyboard

    async def test_concurrent_anchor_anchor_one_wins(self, wired):
        eid = wired["rec"].id
        t1 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "aa"))))
        t2 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "ab"))))
        done, pending = await asyncio.wait({t1, t2}, timeout=0.3)
        assert len(done) == 1 and len(pending) == 1
        assert _body(done.pop().result())["error"] == "question_pending"
        # Drive the winner's relay post → exactly one anchor + one ledger entry.
        winner = pending.pop()
        await wired["seq"].post_for_block(ASK_TOOL, _ANCHOR_HASH)
        r_win = await asyncio.wait_for(winner, timeout=1.0)
        assert _body(r_win)["outcome"] == "anchored"
        assert len(wired["chan"].anchors) == 1
        assert wired["reg"].open_question_numbers(eid) == [1]

    async def test_concurrent_cross_kind_one_wins(self, wired):
        eid = wired["rec"].id
        t_btn = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid, "bx"))))
        t_anc = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "ax"))))
        done, pending = await asyncio.wait({t_btn, t_anc}, timeout=0.3)
        assert len(done) == 1 and len(pending) == 1
        assert _body(done.pop().result())["error"] == "question_pending"
        for t in pending:
            t.cancel()


# ===========================================================================
# marker lifecycle — cleared on every terminal failure path
# ===========================================================================


class TestMarkerLifecycle:
    async def test_marker_cleared_on_delivery_failed_anchor(self, wired):
        eid = wired["rec"].id
        wired["chan"].send_returns_none = True  # wire post returns None
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "af"))))
        resp = await _drive_anchor(wired, task)
        assert _body(resp)["error"] == "delivery_failed"
        assert wired["drv"].ask_inflight(eid) is None

    async def test_marker_cleared_on_success_anchor(self, wired):
        eid = wired["rec"].id
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "as"))))
        resp = await _drive_anchor(wired, task)
        assert _body(resp)["outcome"] == "anchored"
        assert wired["drv"].ask_inflight(eid) is None


# ===========================================================================
# number-allocation failure — absent vs raising (Sol r8-4)
# ===========================================================================


class TestAllocationFailure:
    async def test_raising_allocator_anchor_refuses_before_post(
        self, wired, monkeypatch,
    ):
        eid = wired["rec"].id

        async def _boom(_eid):
            raise RuntimeError("registry down")

        monkeypatch.setattr(wired["reg"], "allocate_question_number", _boom)
        resp = await wired["ask"](_FakeRequest(_anchor_payload(eid, "ar")))
        body = _body(resp)
        assert body["ok"] is False and body["error"] == "internal_error"
        assert wired["chan"].anchors == []       # ZERO wire posts
        assert wired["drv"].ask_inflight(eid) is None
        # Retry short-circuits (reattaches to the tombstoned refusal); still zero.
        resp2 = await wired["ask"](_FakeRequest(_anchor_payload(eid, "ar")))
        assert _body(resp2)["ok"] is False
        assert wired["chan"].anchors == []

    async def test_raising_allocator_button_refuses_before_post(
        self, wired, monkeypatch,
    ):
        eid = wired["rec"].id

        async def _boom(_eid):
            raise RuntimeError("registry down")

        monkeypatch.setattr(wired["reg"], "allocate_question_number", _boom)
        resp = await wired["ask"](_FakeRequest(_btn_payload(eid, "br")))
        body = _body(resp)
        assert body["ok"] is False and body["error"] == "internal_error"
        assert wired["chan"].keyboards == []
        assert wired["drv"].ask_inflight(eid) is None

    async def test_absent_allocator_anchor_legacy_degraded(
        self, wired, monkeypatch,
    ):
        """Absent allocator (degraded mode): un-numbered anchor posts, NO ledger
        entry, marker cleared, one-question invariant UNAVAILABLE (Sol r9-4)."""
        eid = wired["rec"].id
        monkeypatch.setattr(wired["reg"], "allocate_question_number", None)
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "al"))))
        resp = await _drive_anchor(wired, task)
        body = _body(resp)
        assert body["outcome"] == "anchored"
        assert body["question_number"] is None
        assert len(wired["chan"].anchors) == 1          # posted un-numbered
        assert wired["reg"].open_question_numbers(eid) == []  # NO ledger entry
        assert wired["drv"].ask_inflight(eid) is None


# ===========================================================================
# initial-anchor add-failure compensation (Sol r5-5 + r6-1)
# ===========================================================================


class TestAddFailureCompensation:
    async def test_anchor_add_failure_compensates(self, wired, monkeypatch):
        eid, seq = wired["rec"].id, wired["seq"]

        async def _boom(*a, **k):
            raise RuntimeError("ledger down")

        monkeypatch.setattr(wired["reg"], "add_open_question", _boom)
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "ac"))))
        resp = await _drive_anchor(wired, task)
        body = _body(resp)
        # The handler maps the compensated outcome to ok:false internal_error.
        assert body["ok"] is False and body["error"] == "internal_error"
        # The orphan WAS posted (one anchor)...
        assert len(wired["chan"].anchors) == 1
        orphan_mid = wired["chan"].anchors[0][0]
        # ...and withdraw-edited via the RAW wire edit primitive.
        withdraws = [e for e in wired["chan"].edits
                     if e["message_id"] == orphan_mid and "withdrawn" in e["text"]]
        assert withdraws, "no withdraw edit attempted"
        # High-water advanced to the orphan (a later ask opens BELOW it).
        assert seq._high_water == orphan_mid
        # Compensated intent outcome recorded exactly once.
        outcome = wired["drv"].send_intent_outcome(eid, "ac")
        assert outcome is not None
        assert outcome.get("compensated") is True
        assert outcome.get("message_id") == orphan_mid
        # Marker cleared; no ledger entry survived.
        assert wired["drv"].ask_inflight(eid) is None
        assert wired["reg"].open_question_numbers(eid) == []

    async def test_ask_after_compensation_opens_below_orphan(
        self, wired, monkeypatch,
    ):
        eid, seq = wired["rec"].id, wired["seq"]
        calls = {"n": 0}
        orig_add = wired["reg"].add_open_question

        async def _boom_once(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("ledger down")
            return await orig_add(*a, **k)

        monkeypatch.setattr(wired["reg"], "add_open_question", _boom_once)
        t1 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "c1"))))
        await _drive_anchor(wired, t1)
        orphan_mid = wired["chan"].anchors[0][0]
        # A subsequent ask posts BELOW the compensated orphan (higher id).
        t2 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "c2"))))
        await _drive_anchor(wired, t2)
        second_mid = wired["chan"].anchors[1][0]
        assert second_mid > orphan_mid


# ===========================================================================
# post-add generation re-check
# ===========================================================================


class TestMarkerCancellationWedge:
    """B1: a transport CANCELLATION mid number-allocation must clear the ingress
    marker — cleanup previously lived only in ``except Exception`` paths that a
    ``CancelledError`` bypasses, wedging every later ask/reply ``question_pending``
    until restart."""

    async def test_cancel_mid_allocation_clears_marker_anchor(
        self, wired, monkeypatch,
    ):
        eid = wired["rec"].id
        gate = asyncio.Event()

        async def _slow(_eid):
            await gate.wait()
            return 1

        monkeypatch.setattr(wired["reg"], "allocate_question_number", _slow)
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "cx"))))
        await asyncio.sleep(0.02)
        # Marker claimed, coroutine parked in the awaited allocation.
        assert wired["drv"].ask_inflight(eid) == "cx"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # B1: cleared despite the CancelledError.
        assert wired["drv"].ask_inflight(eid) is None
        # A subsequent genuinely-new anchor ask is NOT wedged question_pending
        # (distinct projection hash — the cancelled attempt's unarmed intent is a
        # separate block).
        gate.set()
        t2 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "cy", hash="anchor-hash-2"))))
        resp2 = await _drive_anchor(wired, t2, hash="anchor-hash-2")
        assert _body(resp2)["outcome"] == "anchored"

    async def test_cancel_mid_allocation_clears_marker_button(
        self, wired, monkeypatch,
    ):
        eid = wired["rec"].id
        gate = asyncio.Event()

        async def _slow(_eid):
            await gate.wait()
            return 1

        monkeypatch.setattr(wired["reg"], "allocate_question_number", _slow)
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid, "cx"))))
        await asyncio.sleep(0.02)
        assert wired["drv"].ask_inflight(eid) == "cx"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert wired["drv"].ask_inflight(eid) is None
        # A subsequent button ask is admitted (registers a live broker request),
        # not refused question_pending.
        gate.set()
        t2 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid, "cy"))))
        await asyncio.sleep(0.02)
        await wired["seq"].post_for_block(ASK_TOOL, _BTN_HASH)
        assert wired["broker"].pending(
            namespace="engagement_ask", scope=eid) == ["cy"]
        wired["broker"].deliver(
            namespace="engagement_ask", scope=eid, request_id="cy",
            option_index=0, actor_id=555)
        await asyncio.wait_for(t2, timeout=1.0)
        await wired["broker"].drain_hooks()


class TestCancelledIntentTombstone:
    """§A3 wave 2 — B1/B2: a transport CANCELLATION between arming/registering an
    ask intent and its post must tombstone the intent AND record a ``cancelled``
    outcome, so (B1) the armed intent is no longer matchable (the relay
    consume-cancels it — nothing posts) and a DIFFERENT ask can post exactly
    once, and (B2) a SAME-request_id retry short-circuits to the recorded
    outcome instead of hanging on the transport budget (anchor) / registering a
    fresh broker request (button)."""

    async def test_cancel_after_arm_before_post_tombstones_anchor_intent(
        self, wired,
    ):
        # B1: the anchor is ARMED and awaiting the relay-deferred post (the relay
        # has NOT reached the block — ``post_for_block`` not called yet).
        eid, drv = wired["rec"].id, wired["drv"]
        t1 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a1"))))
        await asyncio.sleep(0.02)
        assert drv.ask_inflight(eid) == "a1"            # marker armed
        assert wired["chan"].anchors == []              # nothing posted yet
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1
        # Marker cleared AND the armed intent tombstoned with a cancelled outcome.
        assert drv.ask_inflight(eid) is None
        assert drv.send_intent_outcome(eid, "a1") == {
            "ok": False, "error": "cancelled"}
        # The relay reaching the (now-tombstoned) block posts NOTHING.
        await wired["seq"].post_for_block(ASK_TOOL, _ANCHOR_HASH)
        assert wired["chan"].anchors == []
        # A second, DIFFERENT ask posts EXACTLY once.
        t2 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a2", hash="anchor-hash-2"))))
        resp2 = await _drive_anchor(wired, t2, hash="anchor-hash-2")
        assert _body(resp2)["outcome"] == "anchored"
        assert len(wired["chan"].anchors) == 1
        # A same-id retry gets the recorded cancelled outcome verbatim (no hang).
        resp_retry = await asyncio.wait_for(
            wired["ask"](_FakeRequest(_anchor_payload(eid, "a1"))), timeout=1.0)
        assert _body(resp_retry) == {"ok": False, "error": "cancelled"}

    async def test_cancel_during_inflight_post_loses_the_post_wins_anchor(
        self, wired, monkeypatch,
    ):
        """A3 · F-ORDER (Sol A3 wave 3 — the final blocker): a transport
        CANCELLATION that lands WHILE the relay is mid-post (holding the writer
        lock inside the poster) must be SERIALIZED behind that post — the cancel
        LOSES, the post WINS, and the ledger ends with exactly ONE live question
        (Sol's [1, 2] two-live-questions repro is closed).

        Interleaving: the relay reaches ``_post_intent_locked`` and blocks INSIDE
        the poster (gated on an Event, holding the writer lock) → the handler task
        is cancelled (its cleanup runs) → a SECOND ask must now be REFUSED
        ``question_pending`` (the cancel lost, so the marker still stands) →
        release the poster (post completes, ledger entry tracked, marker cleared
        at durable ownership) → exactly ONE live question, the intent keeps its
        SUCCESS outcome, and the cancelled handler's same-request_id retry
        reattaches to the POSTED outcome."""
        eid, drv, seq, chan = (
            wired["rec"].id, wired["drv"], wired["seq"], wired["chan"])
        gate = asyncio.Event()
        real_add = wired["reg"].add_open_question

        async def _gated_add(*a, **k):
            await gate.wait()               # park the poster mid-post, lock held
            return await real_add(*a, **k)

        monkeypatch.setattr(wired["reg"], "add_open_question", _gated_add)

        # a1 registers, arms, and parks awaiting the relay-deferred post.
        t1 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a1"))))
        await asyncio.sleep(0.02)
        assert drv.ask_inflight(eid) == "a1"

        # The relay reaches the block: the poster posts the wire message, then
        # blocks in the gated add_open_question — STILL holding the writer lock.
        relay = asyncio.ensure_future(seq.post_for_block(ASK_TOOL, _ANCHOR_HASH))
        await asyncio.sleep(0.02)
        # The poster's wire message has landed but the ledger write (add_open_
        # question) is parked, so the intent is still armed and unresolved.
        intent = seq.registry.by_request_id("a1")
        assert intent.state == "armed"
        assert len(chan.anchors) == 1      # wire message landed; ledger NOT yet
        assert drv._effective_open_question_numbers(eid) == []

        # Cancel the handler. Its cleanup routes through the SERIALIZED cancel,
        # which now BLOCKS on the writer lock the relay holds.
        t1.cancel()
        await asyncio.sleep(0.02)

        # The cancel is stalled behind the post — the marker STILL stands, so a
        # SECOND, different ask is refused question_pending (one-question intact).
        assert drv.ask_inflight(eid) == "a1"
        resp2 = await asyncio.wait_for(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a2", hash="anchor-hash-2"))), timeout=1.0)
        assert _body(resp2)["error"] == "question_pending"

        # Release the poster → it finishes durable ownership (clears the marker)
        # and the post resolves ok; the writer lock frees; the cancel LOSES.
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await t1
        await asyncio.wait_for(relay, timeout=1.0)

        # Exactly ONE live question; the intent keeps its SUCCESS outcome; the
        # marker cleared at durable ownership (the cancel never clobbered it).
        assert len(chan.anchors) == 1
        assert drv.ask_inflight(eid) is None
        outcome = drv.send_intent_outcome(eid, "a1")
        assert outcome["ok"] is True and outcome.get("message_id") is not None
        assert drv._effective_open_question_numbers(eid) == [1]

        # The cancelled handler's same-request_id retry reattaches to the POSTED
        # outcome — no second post, no hang.
        resp_retry = await asyncio.wait_for(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a1"))), timeout=1.0)
        body_retry = _body(resp_retry)
        assert body_retry["outcome"] == "anchored"
        assert body_retry["message_id"] == outcome["message_id"]
        assert len(chan.anchors) == 1

    async def test_cancel_mid_allocation_same_id_retry_short_circuits_anchor(
        self, wired, monkeypatch,
    ):
        # B2 (anchor): cancel WHILE the number allocation is in flight — the
        # PENDING intent must be tombstoned so a SAME-request_id retry does not
        # hang on the transport budget waiting for a never-armed intent.
        eid, drv = wired["rec"].id, wired["drv"]
        gate = asyncio.Event()

        async def _slow(_eid):
            await gate.wait()
            return 1

        monkeypatch.setattr(wired["reg"], "allocate_question_number", _slow)
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "cx"))))
        await asyncio.sleep(0.02)
        assert drv.ask_inflight(eid) == "cx"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert drv.ask_inflight(eid) is None
        assert drv.send_intent_outcome(eid, "cx") == {
            "ok": False, "error": "cancelled"}
        # SAME request_id retry short-circuits to the recorded outcome — no hang,
        # no fresh post.
        gate.set()
        resp = await asyncio.wait_for(
            wired["ask"](_FakeRequest(_anchor_payload(eid, "cx"))), timeout=1.0)
        assert _body(resp) == {"ok": False, "error": "cancelled"}
        assert wired["chan"].anchors == []

    async def test_cancel_mid_allocation_same_id_retry_short_circuits_button(
        self, wired, monkeypatch,
    ):
        # B2 (button): a same-request_id retry must NOT register a fresh broker
        # request (which would burn the full timeout with no keyboard).
        eid, drv = wired["rec"].id, wired["drv"]
        gate = asyncio.Event()

        async def _slow(_eid):
            await gate.wait()
            return 1

        monkeypatch.setattr(wired["reg"], "allocate_question_number", _slow)
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid, "cx"))))
        await asyncio.sleep(0.02)
        assert drv.ask_inflight(eid) == "cx"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert drv.ask_inflight(eid) is None
        assert drv.send_intent_outcome(eid, "cx") == {
            "ok": False, "error": "cancelled"}
        gate.set()
        resp = await asyncio.wait_for(
            wired["ask"](_FakeRequest(_btn_payload(eid, "cx"))), timeout=1.0)
        assert _body(resp) == {"ok": False, "error": "cancelled"}
        # No broker request was created — no timeout burn, no keyboard.
        assert wired["broker"].pending(
            namespace="engagement_ask", scope=eid) == []
        assert wired["chan"].keyboards == []


class TestPostAddGenRecheck:
    async def test_gen_bump_between_reserve_and_add_marks_answered(
        self, wired, monkeypatch,
    ):
        eid, drv = wired["rec"].id, wired["drv"]
        holder = {"gen": 0}
        monkeypatch.setattr(drv, "inbound_generation", lambda _e: holder["gen"])
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "g1"))))
        await asyncio.sleep(0.02)
        # An operator envelope arrives between reserve and the relay post.
        holder["gen"] = 1
        await wired["seq"].post_for_block(ASK_TOOL, _ANCHOR_HASH)
        await asyncio.wait_for(task, timeout=1.0)
        # The racing operator text is the anchor's answer → answered + settled.
        assert drv._effective_open_question_numbers(eid) == []
        # A settle edit ran over the anchor.
        assert wired["chan"].edits, "no settle edit for the gen-bumped anchor"


# ===========================================================================
# A7 · F-ANCHOR — embedded-options anchor refusal (Task 12)
# ===========================================================================


class TestEmbeddedOptionsAnchor:
    async def test_spaced_embedded_lines_refused(self, wired):
        """The LIVE ``A — opt`` free-text form: ≥2 enumerated lines in an anchor
        question → embedded_options with the spec copy, no post, no broker."""
        eid = wired["rec"].id
        q = "Which stack?\nA — Python MCP + MCPB\nB — Rust bridge"
        resp = await wired["ask"](_FakeRequest(_anchor_payload(eid, "e1", question=q)))
        body = _body(resp)
        assert body["ok"] is False
        assert body["error"] == "embedded_options"
        assert "multiple-choice" in body["message"]
        # No wire post, no broker request, no ingress marker held.
        assert wired["chan"].anchors == []
        assert wired["broker"].pending(namespace="engagement_ask", scope=eid) == []
        assert wired["drv"].ask_inflight(eid) is None

    async def test_digit_embedded_lines_refused(self, wired):
        eid = wired["rec"].id
        q = "Pick:\n1. one\n2. two"
        resp = await wired["ask"](_FakeRequest(_anchor_payload(eid, "e2", question=q)))
        assert _body(resp)["error"] == "embedded_options"

    async def test_embedded_refusal_records_intent_and_retry_short_circuits(
        self, wired,
    ):
        """The refusal records the intent OUTCOME so a same-request_id transport
        retry reattaches and short-circuits to the SAME embedded_options."""
        eid, drv = wired["rec"].id, wired["drv"]
        q = "Which?\nA — Python MCP\nB — Rust bridge"
        resp = await wired["ask"](_FakeRequest(_anchor_payload(eid, "er", question=q)))
        assert _body(resp)["error"] == "embedded_options"
        # Recorded on the intent (byte-identical to the live refusal payload).
        assert drv.send_intent_outcome(eid, "er") == {
            "ok": False, "error": "embedded_options",
            "message": _body(resp)["message"],
        }
        # A same-id retry reattaches and returns the recorded outcome verbatim.
        resp2 = await wired["ask"](_FakeRequest(_anchor_payload(eid, "er", question=q)))
        assert _body(resp2) == _body(resp)
        # Still nothing posted.
        assert wired["chan"].anchors == []

    async def test_one_enumerated_line_allowed(self, wired):
        """A single enumerated line is below the ≥2 threshold — a normal anchor."""
        eid = wired["rec"].id
        q = "Which?\nA — Python MCP\njust prose, no second option"
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "ok1", question=q))))
        resp = await _drive_anchor(wired, task)
        assert _body(resp)["ok"] is True
        assert _body(resp)["outcome"] == "anchored"

    async def test_plain_prose_anchor_allowed(self, wired):
        eid = wired["rec"].id
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "ok2", question="What DB name do you want?"))))
        resp = await _drive_anchor(wired, task)
        assert _body(resp)["ok"] is True

    async def test_button_ask_with_enumerated_question_untouched(self, wired):
        """A7 is ANCHORS ONLY — a button ask whose QUESTION looks enumerated
        still posts its keyboard (never refused embedded_options)."""
        eid = wired["rec"].id
        q = "Which?\n1. one\n2. two"
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid, "b7", question=q))))
        resp = await _drive_button(wired, task, "b7")
        assert _body(resp)["outcome"] == "answered"
        assert len(wired["chan"].keyboards) == 1
