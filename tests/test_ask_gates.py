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
        "ask_cancel": handlers["/internal/channel/ask_cancel"],
        "handlers": handlers,
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

        # Cancel the handler. Its cleanup routes through the SYNCHRONOUS cancel
        # (wave 4), which reads the intent's ``posting`` flag (set while the relay
        # holds the writer lock mid-post) and NO-OPS — the post wins, the marker
        # stands, and the finally is gated so it does not clear it.
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


class TestDoubleCancelWave4:
    """Sol A3 wave 4 — the DOUBLE-cancel variant of the intent-cancellation race.

    The wave-3 fix serialized the cancel cleanup by AWAITING the sequencer writer
    lock. A SECOND ``Task.cancel()`` during that await INTERRUPTS the cleanup;
    control then lands in the outer ``finally`` which cleared ``ask_inflight``
    UNCONDITIONALLY — while the bound poster was past wire-send but pre-ledger-add
    (``posting`` True). A second ask was then admitted → two live questions.

    The fix makes the cancel cleanup FULLY SYNCHRONOUS (no awaits ⇒ no
    double-cancel window) and gates the outer ``finally``'s clear on the cancel
    decision: the marker is left to the poster when the post wins."""

    async def test_double_cancel_during_inflight_post_marker_stands_anchor(
        self, wired, monkeypatch,
    ):
        # Poster event-gated AFTER wire-send / BEFORE ledger-add: the intent is
        # ``posting`` (armed, wire message landed, add_open_question parked, writer
        # lock held). A DOUBLE cancel must NOT clear the marker out from under it.
        eid, drv, seq, chan = (
            wired["rec"].id, wired["drv"], wired["seq"], wired["chan"])
        gate = asyncio.Event()
        real_add = wired["reg"].add_open_question

        async def _gated_add(*a, **k):
            await gate.wait()               # park the poster mid-post, lock held
            return await real_add(*a, **k)

        monkeypatch.setattr(wired["reg"], "add_open_question", _gated_add)

        t1 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a1"))))
        await asyncio.sleep(0.02)
        assert drv.ask_inflight(eid) == "a1"

        # The relay reaches the block: the poster posts the wire message, then
        # blocks in the gated add_open_question — STILL holding the writer lock,
        # with ``posting`` set.
        relay = asyncio.ensure_future(seq.post_for_block(ASK_TOOL, _ANCHOR_HASH))
        await asyncio.sleep(0.02)
        intent = seq.registry.by_request_id("a1")
        assert intent.state == "armed" and intent.posting is True
        assert len(chan.anchors) == 1     # wire message landed; ledger NOT yet
        assert drv._effective_open_question_numbers(eid) == []

        # DOUBLE cancel: the first lands in the handler's cleanup; the second
        # lands WHILE the (pre-fix) serialized cleanup AWAITS the writer lock the
        # relay holds — the window that let the outer finally clear the marker
        # mid-post. The synchronous cleanup has no await to interrupt.
        t1.cancel()
        await asyncio.sleep(0.02)
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1

        # The marker STILL stands (the post is winning) — a second, DIFFERENT ask
        # is refused question_pending (pre-fix the marker was cleared → this ask
        # would be admitted, giving two live questions).
        assert drv.ask_inflight(eid) == "a1"
        resp2 = await asyncio.wait_for(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a2", hash="anchor-hash-2"))), timeout=1.0)
        assert _body(resp2)["error"] == "question_pending"

        # Release the poster → durable ownership clears the marker, the post
        # resolves ok; exactly ONE live question remains.
        gate.set()
        await asyncio.wait_for(relay, timeout=1.0)
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

    async def test_double_cancel_mid_allocation_pending_intent_anchor(
        self, wired, monkeypatch,
    ):
        # A pending-intent double-cancel: the cancel-wins path is fully synchronous
        # now (no window). The marker is cleared once, the pending intent is
        # tombstoned with a cancelled outcome, and a second DIFFERENT ask is
        # admitted cleanly (exactly one post).
        eid, drv = wired["rec"].id, wired["drv"]
        gate = asyncio.Event()

        async def _slow(_eid):
            await gate.wait()
            return 1

        monkeypatch.setattr(wired["reg"], "allocate_question_number", _slow)
        t1 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a1"))))
        await asyncio.sleep(0.02)
        assert drv.ask_inflight(eid) == "a1"
        intent = wired["seq"].registry.by_request_id("a1")
        assert intent.state == "pending" and intent.posting is False

        # DOUBLE cancel while parked in the number allocation (pending intent, no
        # in-flight post) — the sync cleanup tombstones exactly once.
        t1.cancel()
        await asyncio.sleep(0.02)
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1
        assert drv.ask_inflight(eid) is None
        assert drv.send_intent_outcome(eid, "a1") == {
            "ok": False, "error": "cancelled"}

        # The relay reaching the tombstoned block posts NOTHING; a second, DIFFERENT
        # ask is admitted cleanly and posts exactly once.
        gate.set()
        await wired["seq"].post_for_block(ASK_TOOL, _ANCHOR_HASH)
        assert wired["chan"].anchors == []
        t2 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a2", hash="anchor-hash-2"))))
        resp2 = await _drive_anchor(wired, t2, hash="anchor-hash-2")
        assert _body(resp2)["outcome"] == "anchored"
        assert len(wired["chan"].anchors) == 1


class TestPosterOwnsClearWave5:
    """Sol A3 wave 5 — the poster OWNS the ``ask_inflight`` clear on EVERY
    non-durable exit.

    Wave 4 gated the handler's outer ``finally`` off when a transport cancel LOST
    to an in-flight post (``_post_wins``): ownership of the marker transfers to
    the winning poster, which clears it at durable ownership. But if the poster
    then FAILS *before* durable ownership (the wire send raises / returns None →
    the intent resolves ok:false), NOTHING cleared the marker: it wedged, and
    every later ask/reply was refused ``question_pending`` until restart.

    The fix wraps each poster body in try/finally so the poster itself clears the
    marker (CAS) on every exit that did not reach durable ownership."""

    async def test_post_wins_then_send_raises_clears_marker_anchor(
        self, wired, monkeypatch,
    ):
        # Sol's exact interleaving: a1 arms and parks awaiting the relay post; the
        # relay reaches the block and the poster blocks INSIDE the gated wire send
        # (posting=True, writer lock held); the handler is cancelled ONCE (the sync
        # cancel reads posting=True → NO-OPS → the post wins, _post_wins=True, the
        # handler's finally is gated OFF); the gate releases with the wire send
        # RAISING → the poster returns None BEFORE durable ownership.
        eid, drv, seq, chan = (
            wired["rec"].id, wired["drv"], wired["seq"], wired["chan"])
        gate = asyncio.Event()
        real_send = chan.send_response_to_topic

        async def _gated_raising_send(topic_id, text):
            await gate.wait()               # park the poster mid-post, lock held
            raise RuntimeError("wire send failed after the cancel lost")

        monkeypatch.setattr(chan, "send_response_to_topic", _gated_raising_send)

        t1 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a1"))))
        await asyncio.sleep(0.02)
        assert drv.ask_inflight(eid) == "a1"

        relay = asyncio.ensure_future(seq.post_for_block(ASK_TOOL, _ANCHOR_HASH))
        await asyncio.sleep(0.02)
        intent = seq.registry.by_request_id("a1")
        assert intent.state == "armed" and intent.posting is True

        # Cancel ONCE — the cancel loses (posting=True), the post wins.
        t1.cancel()
        await asyncio.sleep(0.02)
        # The cancel is stalled behind the post → the marker still stands.
        assert drv.ask_inflight(eid) == "a1"

        # Release → the wire send RAISES → the poster returns None BEFORE durable
        # ownership. Pre-fix: NOTHING clears the marker (handler finally gated off,
        # poster raised out before its durable clear) → wedged. Post-fix: the
        # poster's own finally clears it (CAS).
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await t1
        await asyncio.wait_for(relay, timeout=1.0)

        # Marker cleared; nothing posted; no live question; intent resolved ok:false.
        assert drv.ask_inflight(eid) is None
        assert chan.anchors == []
        assert drv._effective_open_question_numbers(eid) == []
        outcome = drv.send_intent_outcome(eid, "a1")
        assert outcome is not None and outcome["ok"] is False

        # A next, DIFFERENT ask is NOT wedged question_pending — it posts cleanly.
        monkeypatch.setattr(chan, "send_response_to_topic", real_send)
        t2 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a2", hash="anchor-hash-2"))))
        resp2 = await _drive_anchor(wired, t2, hash="anchor-hash-2")
        assert _body(resp2)["outcome"] == "anchored"

        # The cancelled handler's same-request_id retry short-circuits to the
        # recorded failure outcome — no second post, no hang.
        resp_retry = await asyncio.wait_for(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a1"))), timeout=1.0)
        assert _body(resp_retry)["ok"] is False
        assert len(chan.anchors) == 1      # only a2 posted

    async def test_post_wins_then_send_returns_none_clears_marker_anchor(
        self, wired, monkeypatch,
    ):
        # Same race, but the wire send RETURNS None (not int) mid-post rather than
        # raising — another non-durable poster exit that must clear the marker.
        eid, drv, seq, chan = (
            wired["rec"].id, wired["drv"], wired["seq"], wired["chan"])
        gate = asyncio.Event()

        async def _gated_none_send(topic_id, text):
            await gate.wait()
            return None

        monkeypatch.setattr(chan, "send_response_to_topic", _gated_none_send)

        t1 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a1"))))
        await asyncio.sleep(0.02)
        relay = asyncio.ensure_future(seq.post_for_block(ASK_TOOL, _ANCHOR_HASH))
        await asyncio.sleep(0.02)
        assert seq.registry.by_request_id("a1").posting is True

        t1.cancel()
        await asyncio.sleep(0.02)
        assert drv.ask_inflight(eid) == "a1"        # the cancel lost

        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await t1
        await asyncio.wait_for(relay, timeout=1.0)

        assert drv.ask_inflight(eid) is None        # poster owns the clear
        assert drv.send_intent_outcome(eid, "a1")["ok"] is False

    async def test_button_poster_failure_leaves_marker_clear(
        self, wired, monkeypatch,
    ):
        # Button parity: durable ownership for a button ask is the BROKER.register-
        # side marker clear, which runs BEFORE the poster. A poster failure (the
        # keyboard post RAISES) therefore never wedges ask_inflight — and the
        # poster's own finally is a belt-and-suspenders CAS no-op. A next ask is
        # admitted (never refused question_pending).
        eid, drv, chan = wired["rec"].id, wired["drv"], wired["chan"]
        real_kbd = chan.post_options_keyboard

        async def _boom(*, engagement_id, request_id, question, options, **k):
            raise RuntimeError("keyboard post failed")

        monkeypatch.setattr(chan, "post_options_keyboard", _boom)
        t1 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid, "b1"))))
        await asyncio.sleep(0.02)
        await wired["seq"].post_for_block(ASK_TOOL, _BTN_HASH)
        resp1 = await asyncio.wait_for(t1, timeout=1.0)
        assert _body(resp1)["ok"] is False
        # The marker never wedged (cleared at register, before the poster ran).
        assert drv.ask_inflight(eid) is None

        # A next, DIFFERENT button ask is NOT refused question_pending — it drives
        # to an answered resolution.
        monkeypatch.setattr(chan, "post_options_keyboard", real_kbd)
        t2 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid, "b2", hash="btn-hash-2"))))
        resp2 = await _drive_button(wired, t2, "b2", hash="btn-hash-2")
        assert _body(resp2)["outcome"] == "answered"


class TestCompensationCancelInterleaving:
    """F5 (whole-branch gate): a transport cancel LOSES to an in-flight anchor
    post (``posting`` True → the sync cancel no-ops, ``_post_wins``), and THEN
    ``add_open_question`` fails after the wire message landed. The compensation
    path must run (withdraw edit + ``mark_intent_compensated`` ok:false), the
    poster must OWN the ``ask_inflight`` clear (it never reached durable
    ownership), and a next ask must pass. Composes the wave-5 cancel-loses
    harness with the add-failure compensation injection."""

    async def test_cancel_loses_then_add_failure_compensates_and_clears_marker(
        self, wired, monkeypatch,
    ):
        eid, drv, seq, chan = (
            wired["rec"].id, wired["drv"], wired["seq"], wired["chan"])
        gate = asyncio.Event()
        orig_add = wired["reg"].add_open_question

        async def _gated_boom_add(*a, **k):
            # Park the poster mid-post (wire message already landed, writer lock
            # held, posting=True), then FAIL the ledger add.
            await gate.wait()
            raise RuntimeError("ledger down")

        monkeypatch.setattr(wired["reg"], "add_open_question", _gated_boom_add)

        t1 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a1"))))
        await asyncio.sleep(0.02)
        assert drv.ask_inflight(eid) == "a1"

        relay = asyncio.ensure_future(seq.post_for_block(ASK_TOOL, _ANCHOR_HASH))
        await asyncio.sleep(0.02)
        intent = seq.registry.by_request_id("a1")
        assert intent.state == "armed" and intent.posting is True
        assert len(chan.anchors) == 1          # orphan wire message landed
        orphan_mid = chan.anchors[0][0]

        # Cancel ONCE — posting=True → the cancel loses, the post wins, the
        # handler's outer finally is gated off (poster owns the marker clear).
        t1.cancel()
        await asyncio.sleep(0.02)
        assert drv.ask_inflight(eid) == "a1"   # cancel stalled behind the post

        # Release → add_open_question RAISES → compensation runs.
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await t1
        await asyncio.wait_for(relay, timeout=1.0)

        # Compensation: withdraw edit over the orphan (RAW wire edit).
        withdraws = [e for e in chan.edits
                     if e["message_id"] == orphan_mid and "withdrawn" in e["text"]]
        assert withdraws, "no withdraw edit for the compensated orphan"
        # Compensated intent outcome recorded ok:false, once.
        outcome = drv.send_intent_outcome(eid, "a1")
        assert outcome is not None
        assert outcome["ok"] is False and outcome.get("compensated") is True
        assert outcome.get("message_id") == orphan_mid
        # High-water advanced to the orphan; no ledger entry survived.
        assert seq._high_water == orphan_mid
        assert wired["reg"].open_question_numbers(eid) == []
        # Marker cleared — poster-owned (never reached durable ownership).
        assert drv.ask_inflight(eid) is None

        # A next, DIFFERENT ask is NOT wedged question_pending — it posts BELOW
        # the compensated orphan (higher id) and resolves cleanly.
        monkeypatch.setattr(wired["reg"], "add_open_question", orig_add)
        t2 = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _anchor_payload(eid, "a2", hash="anchor-hash-2"))))
        resp2 = await _drive_anchor(wired, t2, hash="anchor-hash-2")
        assert _body(resp2)["outcome"] == "anchored"
        assert chan.anchors[1][0] > orphan_mid


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


class TestCapsRemovedEndToEnd:
    """D1 (round 4, spec §D1 bullets 1-2): the invented length caps
    (``_ASK_MAX_LABEL_LEN``=48, the 1024-char question cap, the 25-char
    ``short`` cap) are gone — a long option label / question / short is
    ACCEPTED and posts a real keyboard end-to-end (never ``invalid_args``).
    Only option COUNT (documented product-contract exception) still rejects.
    """

    async def test_139_char_option_label_accepted(self, wired):
        """The LIVE Q2 failure form: a long, readable option label used to be
        refused ``invalid_args`` at the 48-char cap; it now posts verbatim."""
        eid = wired["rec"].id
        base = "Option A — a genuinely long, readable choice description "
        long_label = base + "x" * (139 - len(base))
        assert len(long_label) == 139
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid, "cap1", options=[long_label, "B"]))))
        resp = await _drive_button(wired, task, "cap1")
        body = _body(resp)
        assert body["ok"] is True
        assert body["outcome"] == "answered"
        # The strip_enumerator normalization removes the leading "Option A — ".
        assert "a genuinely long, readable choice description" in (
            wired["chan"].keyboards[-1][1])

    async def test_2000_char_question_accepted(self, wired):
        long_question = "x" * 2000
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid=wired["rec"].id, rid="cap2", question=long_question))))
        resp = await _drive_button(wired, task, "cap2")
        assert _body(resp)["ok"] is True

    async def test_9_options_still_rejected_count_cap(self, wired):
        """The COUNT cap (documented product-contract exception, unaffected
        by D1) still refuses — this is never about label/question length."""
        eid = wired["rec"].id
        resp = await wired["ask"](_FakeRequest(_btn_payload(
            eid, "cap3", options=[f"o{i}" for i in range(9)])))
        assert _body(resp) == {"ok": False, "error": "invalid_args"}

    async def test_duplicate_full_labels_still_rejected(self, wired):
        eid = wired["rec"].id
        resp = await wired["ask"](_FakeRequest(_btn_payload(
            eid, "cap4", options=["Same", "Same"])))
        assert _body(resp) == {"ok": False, "error": "invalid_args"}

    async def test_blank_and_non_string_short_never_reject(self, wired):
        """A blank or non-string ``short`` used to be refused at the 25-char/
        non-blank checks; it now floors the button set (D2) but still posts."""
        eid = wired["rec"].id
        options = [
            {"label": "Personal Gmail", "short": "   "},
            {"label": "Work Outlook", "short": 7},
        ]
        task = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(eid, "cap5", options=options))))
        resp = await _drive_button(wired, task, "cap5")
        assert _body(resp)["ok"] is True
        assert len(wired["chan"].keyboards) == 1


# ===========================================================================
# D1 (round 4, Task A5) — validation-gate WIRING into the ask handler:
# single-owner validation, terminal-exit publication, cancel-awareness.
# REAL handler + REAL VerdictBroker + REAL OutputSequencer; asyncio.Event
# barriers inside a monkeypatched allocator force the owner/reattacher
# orderings. Never patches ``<module>.asyncio.sleep``.
# ===========================================================================


class TestGateWiring:
    def _barrier_alloc(self, wired, *, raises=False):
        """Patch the registry allocator with a controllable barrier so the
        OWNER blocks mid-validation (after registering the intent + claiming
        the ingress marker, before the post) while a same-request_id
        reattacher blocks on the gate. Returns (entered, release)."""
        entered = asyncio.Event()
        release = asyncio.Event()
        orig = wired["reg"].allocate_question_number

        async def _alloc(eid):
            entered.set()
            await release.wait()
            if raises:
                raise RuntimeError("boom")
            return await orig(eid)

        wired["reg"].allocate_question_number = _alloc
        return entered, release

    async def _launch_owner_reattacher(self, wired, rid, entered, **over):
        """Owner registers first (created intent), blocks in the allocator;
        the reattacher then registers (created=False) and blocks on the gate."""
        owner = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(wired["rec"].id, rid, **over))))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        reattach = asyncio.ensure_future(wired["ask"](_FakeRequest(
            _btn_payload(wired["rec"].id, rid, **over))))
        await asyncio.sleep(0.02)  # let the reattacher reach the gate wait
        assert not reattach.done()  # truly blocked on the PENDING gate
        return owner, reattach

    async def test_reattacher_blocked_gets_internal_error_no_broker_no_post(
        self, wired,
    ):
        """(a) allocator RAISES while a reattacher is blocked on the gate →
        both get ``internal_error`` byte-identically; no broker, no keyboard."""
        entered, release = self._barrier_alloc(wired, raises=True)
        owner, reattach = await self._launch_owner_reattacher(wired, "gw1", entered)
        release.set()
        r_owner = _body(await asyncio.wait_for(owner, 1.0))
        r_re = _body(await asyncio.wait_for(reattach, 1.0))
        assert r_owner == {"ok": False, "error": "internal_error"}
        assert r_re == {"ok": False, "error": "internal_error"}
        assert wired["broker"].pending(
            namespace="engagement_ask", scope=wired["rec"].id) == []
        assert wired["chan"].keyboards == []

    async def test_reattacher_blocked_gets_invalid_args_detail_no_broker_no_post(
        self, wired,
    ):
        """(a) the render-and-measure size check FAILS post-allocation while a
        reattacher is blocked → both get the SAME self-explaining
        ``invalid_args`` detail; no broker, no keyboard."""
        entered, release = self._barrier_alloc(wired)
        owner, reattach = await self._launch_owner_reattacher(
            wired, "gw2", entered, question="Q" * 5000)
        release.set()
        r_owner = _body(await asyncio.wait_for(owner, 1.0))
        r_re = _body(await asyncio.wait_for(reattach, 1.0))
        assert r_owner["error"] == "invalid_args"
        assert "4096" in r_owner["detail"]
        assert r_re == r_owner  # byte-identical detail
        assert wired["broker"].pending(
            namespace="engagement_ask", scope=wired["rec"].id) == []
        assert wired["chan"].keyboards == []

    async def test_cancel_during_allocation_aborts_no_broker_no_post(self, wired):
        """(b) cancel-during-allocation → the owner aborts at the final gate
        check (no broker record, no post, marker cleared); a reattacher blocked
        on the gate wakes CANCELLED."""
        entered, release = self._barrier_alloc(wired)
        owner, reattach = await self._launch_owner_reattacher(wired, "gw3", entered)
        # Cancel lands WHILE the owner is suspended in allocation.
        await wired["ask_cancel"](_FakeRequest(
            {"engagement_id": wired["rec"].id, "request_id": "gw3"}))
        release.set()
        r_owner = _body(await asyncio.wait_for(owner, 1.0))
        r_re = _body(await asyncio.wait_for(reattach, 1.0))
        assert r_owner == {"ok": False, "error": "cancelled"}
        assert r_re == {"ok": False, "error": "cancelled"}
        assert wired["broker"].pending(
            namespace="engagement_ask", scope=wired["rec"].id) == []
        assert wired["chan"].keyboards == []
        # Marker cleared — a later ask is not wedged ``question_pending``.
        assert wired["drv"].ask_inflight(wired["rec"].id) is None

    async def test_cancel_first_then_ask_refuses(self, wired):
        """(c) cancel-first-then-ask → an ``ask_cancel`` that lands BEFORE the
        original /ask latches the gate; the later /ask finds it and refuses
        with no broker, no keyboard."""
        eid = wired["rec"].id
        await wired["ask_cancel"](_FakeRequest(
            {"engagement_id": eid, "request_id": "gw4"}))
        resp = await asyncio.wait_for(
            wired["ask"](_FakeRequest(_btn_payload(eid, "gw4"))), 1.0)
        assert _body(resp) == {"ok": False, "error": "cancelled"}
        assert wired["broker"].pending(
            namespace="engagement_ask", scope=eid) == []
        assert wired["chan"].keyboards == []

    async def test_operator_away_reattacher_byte_equivalent_no_broker(self, wired):
        """(a) pre-allocation exit: operator-away refuses the owner AND a
        same-request_id reattacher byte-identically, with no broker/post."""
        eid = wired["rec"].id
        wired["drv"]._operator_away[eid] = True  # SUSPEND (F-EXPIRE gate reads it)
        r_owner = _body(await wired["ask"](_FakeRequest(_btn_payload(eid, "gw5"))))
        r_re = _body(await wired["ask"](_FakeRequest(_btn_payload(eid, "gw5"))))
        assert r_owner["error"] == "operator_away"
        assert r_re["error"] == "operator_away"
        assert r_owner["message"] == r_re["message"]
        assert wired["broker"].pending(
            namespace="engagement_ask", scope=eid) == []
        assert wired["chan"].keyboards == []

    async def test_question_pending_reattacher_byte_equivalent_no_broker(
        self, wired,
    ):
        """(a) pre-allocation exit: a second DISTINCT ask is refused
        ``question_pending`` while one is live; a same-id reattacher of the
        refused one gets the byte-equivalent refusal, no broker/post."""
        eid = wired["rec"].id
        # A live broker ask makes the engagement pending.
        wired["broker"].register(
            namespace="engagement_ask", scope=eid, request_id="live",
            timeout_s=60, meta={})
        r_owner = _body(await wired["ask"](_FakeRequest(_btn_payload(eid, "gw6"))))
        r_re = _body(await wired["ask"](_FakeRequest(_btn_payload(eid, "gw6"))))
        assert r_owner["error"] == "question_pending"
        assert r_re["error"] == "question_pending"
        assert r_owner == r_re
        # Only the pre-seeded "live" request is pending — gw6 never registered.
        assert wired["broker"].pending(
            namespace="engagement_ask", scope=eid) == ["live"]
        assert wired["chan"].keyboards == []


# ===========================================================================
# D1 (round 4, Task A4) — AskValidationGate: completion slot, cancellation
# latch, wake event, get_or_create_gate, refcounted retention.
#
# Design ref: docs/superpowers/specs/2026-07-16-engagement-ask-labels-
# round4-design.md §D1 "Validator placement + failure hygiene" (Sol
# r9-1/r10-1/r10-2). Pure unit tests against the gate primitive itself —
# NOT wired into the ask/ask_cancel/reattach handlers yet (Task A5). Real
# asyncio throughout; retention tests inject a fake monotonic clock rather
# than patching ``time.monotonic``/``asyncio.sleep``.
# ===========================================================================


import verdict_broker as _verdict_broker_mod
from channels.channel_handlers import (
    _ASK_VALIDATION_OWNERS,
    ASK_GATES,
    AskValidationGate,
    get_or_create_gate,
    maybe_retire_gate,
)


@pytest.fixture(autouse=True)
def _clean_ask_gates():
    """D1 tests own ``ASK_GATES`` / ``_ASK_VALIDATION_OWNERS`` — never leak a
    gate or an owner marker into another test."""
    ASK_GATES.clear()
    _ASK_VALIDATION_OWNERS.clear()
    yield
    ASK_GATES.clear()
    _ASK_VALIDATION_OWNERS.clear()


class _FakeClock:
    """Deterministic monotonic clock the retention tests advance by hand —
    never patches ``time.monotonic`` (the memory-cage rule bars patching
    shared module attributes; this is dependency-injected instead)."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


class TestCompletionSlot:
    """Completion is owner-only and immutable once set (spec §D1 r10-1)."""

    async def test_pending_before_any_terminal(self):
        gate = AskValidationGate()
        assert gate.completion is None
        assert gate.cancelled is False
        assert gate.effective() == ("PENDING", None)
        assert not gate.event.is_set()

    async def test_set_passed_then_effective_passed(self):
        gate = AskValidationGate()
        gate.set_passed()
        assert gate.completion == ("PASSED", None)
        assert gate.effective() == ("PASSED", None)
        assert gate.event.is_set()

    async def test_set_failed_then_effective_failed_with_payload(self):
        gate = AskValidationGate()
        payload = {"ok": False, "error": "invalid_args", "detail": "too long"}
        gate.set_failed(payload)
        assert gate.effective() == ("FAILED", payload)
        assert gate.event.is_set()

    async def test_set_passed_twice_is_a_noop(self):
        gate = AskValidationGate()
        gate.set_passed()
        gate.set_passed()
        assert gate.completion == ("PASSED", None)

    async def test_set_failed_after_passed_does_not_clobber(self):
        """Owner-only immutability: a second (buggy) owner call never
        overwrites the first resolution, regardless of which terminal it
        tries to set."""
        gate = AskValidationGate()
        gate.set_passed()
        gate.set_failed({"ok": False, "error": "invalid_args"})
        assert gate.completion == ("PASSED", None)

    async def test_set_passed_after_failed_does_not_clobber(self):
        gate = AskValidationGate()
        payload = {"ok": False, "error": "invalid_args"}
        gate.set_failed(payload)
        gate.set_passed()
        assert gate.completion == ("FAILED", payload)


class TestCancellationLatch:
    """Cancellation is monotonic and settable by ANYONE at ANY time —
    including after PASSED (the r9-1 transition a single once-only slot
    cannot express)."""

    async def test_cancel_before_any_completion(self):
        gate = AskValidationGate()
        gate.set_cancelled()
        assert gate.effective() == ("CANCELLED", None)
        assert gate.event.is_set()

    async def test_passed_then_cancelled_effective_is_cancelled(self):
        """The r9-1 transition: PASSED -> CANCELLED. CANCELLED wins over a
        completion that already landed."""
        gate = AskValidationGate()
        gate.set_passed()
        gate.set_cancelled()
        assert gate.completion == ("PASSED", None)  # completion untouched
        assert gate.cancelled is True
        assert gate.effective() == ("CANCELLED", None)

    async def test_failed_then_cancelled_effective_is_cancelled(self):
        gate = AskValidationGate()
        gate.set_failed({"ok": False, "error": "invalid_args"})
        gate.set_cancelled()
        assert gate.effective() == ("CANCELLED", None)

    async def test_cancel_is_idempotent(self):
        gate = AskValidationGate()
        gate.set_cancelled()
        gate.set_cancelled()
        assert gate.cancelled is True
        assert gate.effective() == ("CANCELLED", None)

    async def test_cancel_then_set_passed_still_reads_cancelled(self):
        """Cancellation is never gated by completion order: even a
        (belated) owner PASSED after the latch is set does not un-cancel
        the effective outcome."""
        gate = AskValidationGate()
        gate.set_cancelled()
        gate.set_passed()
        assert gate.completion == ("PASSED", None)
        assert gate.effective() == ("CANCELLED", None)


class TestWakeEvent:
    """ONE asyncio.Event, set by whichever terminal arrives first, wakes a
    waiter regardless of which terminal it was (r10-1)."""

    async def test_latch_only_cancellation_wakes_a_pending_waiter(self):
        gate = AskValidationGate()
        waiter = asyncio.ensure_future(gate.event.wait())
        await asyncio.sleep(0.01)
        assert not waiter.done()
        gate.set_cancelled()
        await asyncio.wait_for(waiter, timeout=1.0)
        assert waiter.result() is True
        assert gate.effective() == ("CANCELLED", None)

    async def test_set_passed_wakes_a_pending_waiter(self):
        gate = AskValidationGate()
        waiter = asyncio.ensure_future(gate.event.wait())
        await asyncio.sleep(0.01)
        gate.set_passed()
        await asyncio.wait_for(waiter, timeout=1.0)
        assert gate.effective() == ("PASSED", None)

    async def test_failed_payload_delivered_byte_identical_to_two_concurrent_waiters(
        self,
    ):
        gate = AskValidationGate()
        payload = {
            "ok": False, "error": "invalid_args",
            "detail": "rendered question+options would exceed Telegram's "
                       "4096-char message limit",
        }

        async def _wait_and_read():
            await gate.event.wait()
            return gate.effective()

        w1 = asyncio.ensure_future(_wait_and_read())
        w2 = asyncio.ensure_future(_wait_and_read())
        await asyncio.sleep(0.01)
        gate.set_failed(payload)
        outcome1 = await asyncio.wait_for(w1, timeout=1.0)
        outcome2 = await asyncio.wait_for(w2, timeout=1.0)
        assert outcome1 == ("FAILED", payload)
        assert outcome2 == ("FAILED", payload)
        # Byte-identical — the SAME dict object, never a re-serialized copy.
        assert outcome1[1] is payload
        assert outcome2[1] is payload

    async def test_a_late_waiter_after_resolution_never_blocks(self):
        """A reattacher that arrives AFTER the gate resolved finds the
        latch already set — ``event.wait()`` returns immediately."""
        gate = AskValidationGate()
        gate.set_failed({"ok": False, "error": "invalid_args"})
        await asyncio.wait_for(gate.event.wait(), timeout=0.05)
        assert gate.effective() == ("FAILED", {"ok": False, "error": "invalid_args"})


class TestGetOrCreateGate:
    """Idempotent per-request_id lookup — the cancel-first ordering the
    r5-1/r10-1 handshake depends on."""

    async def test_first_call_creates_a_gate(self):
        assert "rid-1" not in ASK_GATES
        gate = get_or_create_gate("rid-1")
        assert ASK_GATES["rid-1"] is gate
        assert gate.effective() == ("PENDING", None)

    async def test_second_call_returns_the_same_instance(self):
        g1 = get_or_create_gate("rid-2")
        g2 = get_or_create_gate("rid-2")
        assert g1 is g2

    async def test_different_request_ids_get_different_gates(self):
        g1 = get_or_create_gate("rid-3a")
        g2 = get_or_create_gate("rid-3b")
        assert g1 is not g2

    async def test_cancel_first_then_ask_path_finds_latch_already_set(self):
        """The r5-1 scenario: ``ask_cancel`` is a SEPARATE HTTP request from
        the caller's ``finally`` and can land BEFORE the original ``/ask``
        creates anything. ``ask_cancel`` calls ``get_or_create_gate`` and
        sets the cancellation latch; when the ask path LATER calls
        ``get_or_create_gate`` for the same request_id, it must find the
        SAME gate with the latch already set — never a fresh PENDING gate
        that lets the ask proceed to register a broker request for a
        caller who already abandoned it."""
        rid = "rid-cancel-first"
        cancel_gate = get_or_create_gate(rid)
        cancel_gate.set_cancelled()

        # ... time passes; the original /ask handler now runs for the
        # first time and looks up the gate for the same request_id.
        ask_gate = get_or_create_gate(rid)

        assert ask_gate is cancel_gate
        assert ask_gate.effective() == ("CANCELLED", None)
        # The owner's later PASSED must not resurrect a stale ordering —
        # cancellation still wins.
        ask_gate.set_passed()
        assert ask_gate.effective() == ("CANCELLED", None)


class TestRefcountedRetention:
    """Retention: a gate retires only when (a) no active reference remains
    AND (b) the reattach retention bound has elapsed since terminal
    resolution — reusing ``verdict_broker``'s existing tombstone window
    rather than a new TTL constant (spec §D1 "Tombstone retention, stated
    precisely")."""

    async def test_pending_gate_is_never_retirable(self):
        gate = AskValidationGate()
        assert gate.retirable() is False

    async def test_unresolved_but_referenced_gate_is_never_retirable(self, monkeypatch):
        monkeypatch.setattr(_verdict_broker_mod, "_RETIRE_S", 0.0)
        gate = AskValidationGate()
        gate.acquire()
        assert gate.retirable() is False

    async def test_not_retirable_while_referenced_even_after_terminal(
        self, monkeypatch,
    ):
        monkeypatch.setattr(_verdict_broker_mod, "_RETIRE_S", 0.0)
        clock = _FakeClock()
        gate = AskValidationGate(clock=clock)
        gate.acquire()
        gate.set_passed()
        clock.advance(1000.0)  # WAY past any bound
        assert gate.refcount == 1
        assert gate.retirable() is False  # still referenced

    async def test_retirable_immediately_after_release_when_bound_is_zero(
        self, monkeypatch,
    ):
        monkeypatch.setattr(_verdict_broker_mod, "_RETIRE_S", 0.0)
        clock = _FakeClock()
        gate = AskValidationGate(clock=clock)
        gate.acquire()
        gate.set_passed()
        assert gate.retirable() is False
        gate.release()
        assert gate.refcount == 0
        assert gate.retirable() is True

    async def test_not_retirable_before_the_bound_elapses(self, monkeypatch):
        monkeypatch.setattr(_verdict_broker_mod, "_RETIRE_S", 60.0)
        clock = _FakeClock()
        gate = AskValidationGate(clock=clock)
        gate.set_failed({"ok": False, "error": "invalid_args"})
        clock.advance(59.9)
        assert gate.retirable() is False

    async def test_retirable_once_the_bound_elapses(self, monkeypatch):
        monkeypatch.setattr(_verdict_broker_mod, "_RETIRE_S", 60.0)
        clock = _FakeClock()
        gate = AskValidationGate(clock=clock)
        gate.set_failed({"ok": False, "error": "invalid_args"})
        clock.advance(60.0)
        assert gate.retirable() is True

    async def test_release_never_goes_negative(self):
        gate = AskValidationGate()
        gate.release()
        gate.release()
        assert gate.refcount == 0

    async def test_explicit_bound_overrides_the_reused_default(self):
        """``retirable(bound=...)`` lets a caller override the reused
        window directly — used by :func:`maybe_retire_gate`'s callers in
        tests without touching ``verdict_broker`` at all."""
        clock = _FakeClock()
        gate = AskValidationGate(clock=clock)
        gate.set_passed()
        assert gate.retirable(bound=10.0) is False
        clock.advance(10.0)
        assert gate.retirable(bound=10.0) is True

    async def test_maybe_retire_gate_noop_while_referenced(self, monkeypatch):
        monkeypatch.setattr(_verdict_broker_mod, "_RETIRE_S", 0.0)
        clock = _FakeClock()
        gate = get_or_create_gate("rid-retain", clock=clock)
        gate.acquire()
        gate.set_passed()
        assert maybe_retire_gate("rid-retain") is False
        assert "rid-retain" in ASK_GATES

    async def test_maybe_retire_gate_removes_from_ask_gates_after_release_and_bound(
        self, monkeypatch,
    ):
        monkeypatch.setattr(_verdict_broker_mod, "_RETIRE_S", 5.0)
        clock = _FakeClock()
        gate = get_or_create_gate("rid-retire", clock=clock)
        gate.acquire()
        gate.set_passed()
        assert maybe_retire_gate("rid-retire") is False  # still referenced

        gate.release()
        assert maybe_retire_gate("rid-retire") is False  # bound not elapsed

        clock.advance(5.0)
        assert maybe_retire_gate("rid-retire") is True
        assert "rid-retire" not in ASK_GATES

    async def test_maybe_retire_gate_unknown_request_id_is_a_noop(self):
        assert maybe_retire_gate("no-such-request-id") is False

    async def test_reuses_verdict_broker_retire_window_not_a_new_constant(
        self, monkeypatch,
    ):
        """Direct check that the gate's DEFAULT bound tracks
        ``verdict_broker._RETIRE_S`` dynamically (read at call time, not
        captured at import) — the same guarantee that module documents for
        its own tombstone retirement."""
        clock = _FakeClock()
        gate = AskValidationGate(clock=clock)
        gate.set_passed()

        monkeypatch.setattr(_verdict_broker_mod, "_RETIRE_S", 42.0)
        clock.advance(41.9)
        assert gate.retirable() is False
        clock.advance(0.2)
        assert gate.retirable() is True
