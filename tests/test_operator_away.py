"""F-EXPIRE (v0.83.0, A2a) — operator-away suspend-on-expiry.

When a live ``ask`` keyboard expires unanswered (``verdict_broker`` timeout →
``no_answer``), the engagement now SUSPENDS (operator-away) instead of letting
the agent re-ask in a loop (live incident: 21 asks). This exercises the whole
contract with a REAL ``VerdictBroker`` + REAL ``OutputSequencer`` + REAL
``SummaryController`` + REAL ``_InboundSpool`` (fake wire fns, injected clocks):

* expiry → enriched PAUSED response + the driver away flag set (generation-CAS);
* a racing inbound (generation bump) suppresses the away entry;
* a same-request_id retry against the retired ``no_answer`` tombstone reads the
  ORIGINAL meta generation, so a post-inbound retry cannot re-wedge;
* both ask kinds refused while away with ZERO broker registrations;
* a durable inbound clears the state + resets the away-refusal counter;
* the summary coerces working/waiting → ⏸ while away and restores after clear,
  with the away sample taken AFTER the revision allocation await (Sol r2-3).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self) -> None:
        self.options_keyboards: list[dict] = []
        self.sent_texts: list[tuple] = []
        self.edits: list[dict] = []
        self._next_id = 9000

    async def post_options_keyboard(
        self, *, engagement_id, request_id, question, options,
    ) -> int | None:
        self.options_keyboards.append(
            {"question": question, "options": list(options),
             "request_id": request_id})
        mid = self._next_id
        self._next_id += 1
        return mid

    async def send_response_to_topic(self, topic_id, text) -> int:
        self.sent_texts.append((topic_id, text))
        mid = self._next_id
        self._next_id += 1
        return mid

    async def edit_topic_message(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        self.edits.append(
            {"message_id": message_id, "text": text,
             "clear_keyboard": clear_keyboard})
        return True


def _trivial_spool(tmp_path, eid):
    """A REAL ``_InboundSpool`` with trivial injected primitives (its generation
    counter drives the operator-away CAS; enqueue bumps it)."""
    from drivers.claude_code_driver import _InboundSpool

    async def _wf(_text):
        return True

    async def _notice(_text, _reply_to):
        return True

    return _InboundSpool(
        engagement_id=eid, spool_path=str(tmp_path / f"{eid}.spool.jsonl"),
        write_fifo=_wf, send_notice=_notice)


class _AwayDriver:
    """Fake ``claude_code`` driver: REAL ``OutputSequencer`` (relay-on-arm
    simulated, as production would post at the tool_use block) + REAL
    ``_InboundSpool`` for the generation counter + the operator-away API under
    test. ``note_operator_away`` performs the exact generation-CAS against the
    real spool."""

    def __init__(self, tmp_path, engagement_id: str, topic_id: int = 42) -> None:
        from channels.output_sequencer import OutputSequencer

        self.eid = engagement_id
        self._relay_tasks: list = []
        self._operator_away: dict[str, bool] = {}
        self._away_refusals: dict[str, int] = {}
        self.waiting_calls: list[str] = []
        self.recompute_calls: list[str] = []
        self.ask_refusals = 0

        async def _noop_send(topic, text, reply_to=None):
            return None

        async def _noop_edit(topic, mid, text):
            return True

        self.seq = OutputSequencer(
            engagement_id=engagement_id, topic_id=topic_id,
            send_message=_noop_send, edit_message=_noop_edit)
        self.spool = _trivial_spool(tmp_path, engagement_id)

    # -- inbound-gate reads (called directly by _make_ask) -----------------
    def inbound_unread_depth(self, eid) -> int:
        return self.spool.unread_depth()

    def inbound_generation(self, eid) -> int:
        return self.spool.generation()

    def record_ask_refusal(self, eid) -> int:
        self.ask_refusals += 1
        return self.ask_refusals

    # -- F-EXPIRE operator-away API ----------------------------------------
    def note_operator_away(self, eid, gen) -> bool:
        if self.spool.generation() != gen:
            return False
        self._operator_away[eid] = True
        return True

    def operator_away_active(self, eid) -> bool:
        return self._operator_away.get(eid, False)

    def record_away_refusal(self, eid) -> int:
        n = self._away_refusals.get(eid, 0) + 1
        self._away_refusals[eid] = n
        return n

    # -- lifecycle hooks (getattr-optional in the handler) -----------------
    async def note_ask_waiting(self, eid) -> None:
        self.waiting_calls.append(eid)

    async def recompute_engagement_status(self, eid) -> None:
        self.recompute_calls.append(eid)

    # -- discrete-intent seam (delegates to the real sequencer registry) ---
    def register_send_intent(self, *, engagement_id, request_id, tool_name,
                             projection_hash, poster):
        return self.seq.register_intent(
            request_id=request_id, tool_name=tool_name,
            projection_hash=projection_hash, poster=poster)

    def set_send_intent_poster(self, eid, rid, poster):
        return self.seq.set_intent_poster(rid, poster)

    def arm_send_intent(self, eid, rid):
        intent = self.seq.arm_intent(rid)
        if intent is not None:
            self._relay_tasks.append(asyncio.ensure_future(
                self.seq.post_for_block(intent.tool_name, intent.projection_hash)))
        return intent

    def cancel_send_intent(self, eid, rid):
        return self.seq.cancel_intent(rid)

    def record_send_intent_refusal(self, eid, rid, outcome):
        return self.seq.record_intent_refusal(rid, outcome)

    def send_intent_outcome(self, eid, rid):
        return self.seq.intent_outcome(rid)

    async def mark_send_intent_posted(self, eid, rid, mid):
        return await self.seq.mark_intent_posted(rid, mid)

    async def await_send_intent(self, eid, rid, timeout=None):
        if self._relay_tasks:
            await asyncio.gather(*self._relay_tasks, return_exceptions=True)
            self._relay_tasks.clear()
        return await self.seq.await_intent_resolution(rid, timeout)


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def _body(resp: web.Response) -> dict:
    return json.loads(resp.text)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_broker(monkeypatch):
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture
async def env(tmp_path, fresh_broker, monkeypatch):
    from engagement_registry import EngagementRegistry
    from channels.channel_handlers import _make_channel_handlers

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    ch = _FakeChannel()
    driver = _AwayDriver(tmp_path, rec.id)
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)

    handlers = _make_channel_handlers(telegram_channel=ch, engagement_registry=reg)
    return {
        "reg": reg, "rec": rec, "ch": ch, "driver": driver,
        "broker": fresh_broker, "ask": handlers["/internal/channel/ask"],
    }


def _payload(eid, **over) -> dict:
    base = {
        "engagement_id": eid, "request_id": "rid-1",
        "question": "Proceed?", "options": ["A", "B"], "timeout_s": 60,
        "projection_hash": "hash-abc",
    }
    base.update(over)
    return base


async def _run_until_timeout(env, payload, key_rid):
    """Drive a button ask, let the relay post, fire the broker timeout, return
    the handler response."""
    task = asyncio.ensure_future(env["ask"](_FakeRequest(payload)))
    await asyncio.sleep(0.02)
    env["broker"]._on_timeout(("engagement_ask", env["rec"].id, key_rid))
    resp = await asyncio.wait_for(task, timeout=1.0)
    await env["broker"].drain_hooks()
    return resp


# ---------------------------------------------------------------------------
# 1. no_answer → enriched PAUSED response + away flag set
# ---------------------------------------------------------------------------


async def test_no_answer_enters_operator_away_and_returns_paused(env):
    eid = env["rec"].id
    resp = await _run_until_timeout(
        env, _payload(eid, request_id="e1"), "e1")

    body = _body(resp)
    assert body["ok"] is True
    assert body["outcome"] == "no_answer"
    assert body["engagement_paused"] is True
    assert "PAUSED" in body["message"]
    assert "re-ask" in body["message"] or "Do NOT re-ask" in body["message"]
    # The driver away flag was set via the generation-CAS.
    assert env["driver"].operator_away_active(eid) is True


# ---------------------------------------------------------------------------
# 2. generation-CAS: a racing inbound suppresses the away entry
# ---------------------------------------------------------------------------


async def test_racing_inbound_suppresses_away_entry(env):
    eid = env["rec"].id
    driver = env["driver"]

    task = asyncio.ensure_future(env["ask"](
        _FakeRequest(_payload(eid, request_id="e2"))))
    await asyncio.sleep(0.02)
    # A REAL inbound envelope lands (generation 0 → 1) in the race window
    # BEFORE the timeout finishes — the meta still carries gen 0.
    await driver.spool.enqueue("operator says hi")
    env["broker"]._on_timeout(("engagement_ask", eid, "e2"))
    resp = await asyncio.wait_for(task, timeout=1.0)
    await env["broker"].drain_hooks()

    body = _body(resp)
    # The response is still the enriched PAUSED contract (the operator's inbound
    # will start the next turn — same instruction holds)...
    assert body["engagement_paused"] is True
    # ...but the away FLAG was NOT set: current gen (1) != meta gen (0).
    assert driver.operator_away_active(eid) is False


# ---------------------------------------------------------------------------
# 3. reattach against the retired tombstone uses the ORIGINAL meta gen
# ---------------------------------------------------------------------------


async def test_reattach_tombstone_uses_original_gen_no_rewedge(env):
    eid = env["rec"].id
    driver = env["driver"]

    # First ask expires → away set at generation 0; a tombstone retains its meta
    # (inbound_gen == 0).
    resp1 = await _run_until_timeout(env, _payload(eid, request_id="r1"), "r1")
    assert _body(resp1)["engagement_paused"] is True
    assert driver.operator_away_active(eid) is True

    # An inbound arrives (gen 0 → 1) and clears the away state (simulated).
    await driver.spool.enqueue("operator reply")
    driver._operator_away.pop(eid, None)
    assert driver.inbound_generation(eid) == 1

    # A same-request_id retry reattaches to the retired no_answer tombstone. The
    # tombstone meta carries the ORIGINAL gen (0), so the CAS (current gen 1 !=
    # 0) FAILS — the retry cannot re-wedge the cleared away state.
    resp2 = await env["ask"](_FakeRequest(_payload(eid, request_id="r1")))
    body2 = _body(resp2)
    assert body2["outcome"] == "no_answer"
    assert body2["engagement_paused"] is True
    assert driver.operator_away_active(eid) is False


# ---------------------------------------------------------------------------
# 4/5. both ask kinds refused while away, with ZERO broker registrations
# ---------------------------------------------------------------------------


async def test_button_ask_refused_while_away_no_broker(env):
    eid = env["rec"].id
    driver = env["driver"]
    driver._operator_away[eid] = True

    resp = await env["ask"](_FakeRequest(_payload(eid, request_id="b1")))
    body = _body(resp)
    assert body["ok"] is False
    assert body["error"] == "operator_away"
    assert "END YOUR TURN" in body["message"]
    assert "silently" in body["message"]
    # No broker request registered, no keyboard posted.
    assert env["broker"].pending(namespace="engagement_ask", scope=eid) == []
    assert env["ch"].options_keyboards == []
    # The per-episode away-refusal counter was bumped (Task 5 reads it).
    assert driver._away_refusals.get(eid) == 1


async def test_anchor_ask_refused_while_away_no_broker(env):
    eid = env["rec"].id
    driver = env["driver"]
    driver._operator_away[eid] = True

    resp = await env["ask"](_FakeRequest(
        _payload(eid, request_id="a1", options=[])))
    body = _body(resp)
    assert body["ok"] is False
    assert body["error"] == "operator_away"
    assert "END YOUR TURN" in body["message"] and "silently" in body["message"]
    # No anchor posted, no broker request.
    assert env["ch"].sent_texts == []
    assert env["broker"].pending(namespace="engagement_ask", scope=eid) == []


# ---------------------------------------------------------------------------
# 6. inbound clears the away state + resets the refusal counter (driver seam)
# ---------------------------------------------------------------------------


async def test_inbound_enqueue_clears_away_and_resets_counter(tmp_path):
    from drivers.claude_code_driver import ClaudeCodeDriver, _InboundSpool
    from unittest.mock import AsyncMock

    eid = "engABCDEF01234567"
    drv = ClaudeCodeDriver(
        engagements_root=str(tmp_path), send_to_topic=AsyncMock(),
        casa_framework_mcp_url="x")

    async def _wf(_t):
        return True

    async def _notice(_t, _r):
        return True

    spool = _InboundSpool(
        engagement_id=eid, spool_path=str(tmp_path / "s.jsonl"),
        write_fifo=_wf, send_notice=_notice,
        on_operator_enqueued=lambda: drv._clear_operator_away(eid))
    drv._inbound[eid] = spool

    drv._operator_away[eid] = True
    drv._away_refusals[eid] = 2
    assert drv.operator_away_active(eid) is True

    # A durable operator envelope ends the away episode.
    await spool.enqueue("operator is back")

    assert drv.operator_away_active(eid) is False
    assert eid not in drv._away_refusals
    # A fresh ask is now allowed again (the gate reads the cleared flag).
    assert drv.operator_away_active(eid) is False

    # The INITIAL prompt (is_initial) must NEVER clear or trip the seam.
    drv._operator_away[eid] = True
    await spool.enqueue("the task brief", is_initial=True)
    assert drv.operator_away_active(eid) is True


async def test_note_operator_away_cas_with_real_spool(tmp_path):
    from drivers.claude_code_driver import ClaudeCodeDriver, _InboundSpool
    from unittest.mock import AsyncMock

    eid = "engFEDCBA98765432"
    drv = ClaudeCodeDriver(
        engagements_root=str(tmp_path), send_to_topic=AsyncMock(),
        casa_framework_mcp_url="x")

    async def _wf(_t):
        return True

    async def _notice(_t, _r):
        return True

    spool = _InboundSpool(
        engagement_id=eid, spool_path=str(tmp_path / "s.jsonl"),
        write_fifo=_wf, send_notice=_notice)
    drv._inbound[eid] = spool

    assert spool.generation() == 0
    assert await drv.note_operator_away(eid, 0) is True
    assert drv.operator_away_active(eid) is True

    drv._operator_away.clear()
    await spool.enqueue("inbound")          # generation 0 → 1
    # A stale generation fails the CAS...
    assert await drv.note_operator_away(eid, 0) is False
    assert drv.operator_away_active(eid) is False
    # ...the current generation passes.
    assert await drv.note_operator_away(eid, 1) is True


# ---------------------------------------------------------------------------
# 7/8. summary coercion → ⏸ (REAL SummaryController via the real sequencer)
# ---------------------------------------------------------------------------


class _FakeReg:
    def __init__(self):
        self.rev = 0
        self.open: list[int] = []
        self._gate: asyncio.Event | None = None
        self._on_alloc = None

    async def allocate_summary_revision(self, eid):
        if self._gate is not None:
            if self._on_alloc is not None:
                self._on_alloc()
            await self._gate.wait()
        r = self.rev
        self.rev += 1
        return r

    def open_question_numbers(self, eid):
        return list(self.open)


async def _driver_with_summary(tmp_path):
    from drivers.claude_code_driver import ClaudeCodeDriver
    from unittest.mock import AsyncMock

    edits: list[tuple[int, str]] = []

    async def edit(topic_id, mid, text):
        edits.append((mid, text))
        return True

    reg = _FakeReg()
    drv = ClaudeCodeDriver(
        engagements_root=str(tmp_path), send_to_topic=AsyncMock(return_value=1),
        casa_framework_mcp_url="x", edit_topic_message=edit, registry=reg)
    from engagement_registry import EngagementRecord
    rec = EngagementRecord(
        id="abc12345def67890", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=999,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None,
        origin={"channel": "telegram", "chat_id": "42"}, task="say hello")
    rec.summary_message_id = 500
    ctrl = drv._ensure_summary(rec)
    ctrl.adopt_message_id(500)
    return drv, rec, ctrl, edits, reg


async def test_summary_coerces_to_paused_while_away_and_restores(tmp_path):
    from drivers.summary_controller import (
        STATUS_PAUSED, STATUS_WAITING_REPLY, STATUS_WORKING,
    )
    drv, rec, ctrl, edits, reg = await _driver_with_summary(tmp_path)
    eid = rec.id
    drv._turn_running[eid] = True

    # While away, a turn-end ⏳ waiting submission is coerced to ⏸.
    drv._operator_away[eid] = True
    await drv._summary_status_transition(eid, STATUS_WAITING_REPLY)
    assert ctrl._status == STATUS_PAUSED
    assert edits[-1][1].startswith(STATUS_PAUSED)

    # A ⚙️ working submission is coerced too.
    await drv._summary_status_transition(eid, STATUS_WORKING)
    assert ctrl._status == STATUS_PAUSED

    # Clear + recompute restores the real status (turn still running, no open
    # questions ⇒ ⚙️ working).
    drv._operator_away[eid] = False
    await drv.recompute_engagement_status(eid)
    assert ctrl._status == STATUS_WORKING
    ctrl.shutdown()


async def test_summary_coercion_samples_after_allocation(tmp_path):
    """Sol r2-3 pinned invariant: the away sample is taken AFTER the revision
    allocation await returns. A flag flipped DURING the await must be reflected
    in the final write — proving it is not sampled before the await."""
    from drivers.summary_controller import STATUS_PAUSED, STATUS_WAITING_REPLY
    drv, rec, ctrl, edits, reg = await _driver_with_summary(tmp_path)
    eid = rec.id

    gate = asyncio.Event()
    entered = asyncio.Event()
    reg._gate = gate
    reg._on_alloc = entered.set

    # Away is FALSE when the transition starts; sampling-before-await would read
    # False → ⏳ waiting.
    drv._operator_away[eid] = False
    t = asyncio.ensure_future(
        drv._summary_status_transition(eid, STATUS_WAITING_REPLY))
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    # Flip to away WHILE suspended in the allocation await.
    drv._operator_away[eid] = True
    gate.set()
    await asyncio.wait_for(t, timeout=1.0)

    # The final write reflects the POST-allocation state (away True → ⏸).
    assert ctrl._status == STATUS_PAUSED
    ctrl.shutdown()


# ---------------------------------------------------------------------------
# 9. refusal / paused copies carry the doctrine wording
# ---------------------------------------------------------------------------


async def test_refusal_and_paused_copies_wording():
    from channels.channel_handlers import (
        _ASK_AWAY_REFUSAL, _ASK_PAUSED_MESSAGE,
    )
    assert "END YOUR TURN" in _ASK_AWAY_REFUSAL
    assert "silently" in _ASK_AWAY_REFUSAL
    assert "PAUSED" in _ASK_PAUSED_MESSAGE
    assert "silently" in _ASK_PAUSED_MESSAGE


# ---------------------------------------------------------------------------
# Finding 1: an away-refused ask records a refusal OUTCOME, so a same-request_id
# transport retry short-circuits to the SAME refusal — never awaiting the dead
# intent (delivery_failed) nor re-registering a fresh broker request (timeout).
# ---------------------------------------------------------------------------


async def test_button_away_refusal_retry_short_circuits_no_broker(env):
    eid = env["rec"].id
    driver = env["driver"]
    driver._operator_away[eid] = True

    # First away-refused button ask records the refusal outcome on the intent.
    resp1 = await env["ask"](_FakeRequest(_payload(eid, request_id="b1")))
    assert _body(resp1)["error"] == "operator_away"
    assert driver._away_refusals.get(eid) == 1

    # Same-request_id transport retry: reattaches to the recorded refusal
    # WITHOUT touching the broker and WITHOUT re-bumping the away counter.
    resp2 = await env["ask"](_FakeRequest(_payload(eid, request_id="b1")))
    body2 = _body(resp2)
    assert body2["ok"] is False
    assert body2["error"] == "operator_away"
    assert "END YOUR TURN" in body2["message"]
    # Zero broker registrations across BOTH attempts; no delivery_failed.
    assert env["broker"].pending(namespace="engagement_ask", scope=eid) == []
    assert env["ch"].options_keyboards == []
    assert driver._away_refusals.get(eid) == 1   # retry did NOT re-bump


async def test_anchor_away_refusal_retry_short_circuits_no_delivery_failed(env):
    eid = env["rec"].id
    driver = env["driver"]
    driver._operator_away[eid] = True

    resp1 = await env["ask"](_FakeRequest(
        _payload(eid, request_id="a1", options=[])))
    assert _body(resp1)["error"] == "operator_away"

    # Retry reattaches to the recorded operator_away refusal — NOT delivery_failed.
    resp2 = await env["ask"](_FakeRequest(
        _payload(eid, request_id="a1", options=[])))
    body2 = _body(resp2)
    assert body2["ok"] is False
    assert body2["error"] == "operator_away"
    assert env["ch"].sent_texts == []
    assert env["broker"].pending(namespace="engagement_ask", scope=eid) == []


# ---------------------------------------------------------------------------
# Finding 2: the waiter uses ITS OWN req.meta gen, never BROKER.get_meta by key
# (a retired/reused tombstone key could hand back None or a NEWER generation).
# ---------------------------------------------------------------------------


async def test_no_answer_reads_waiter_req_meta_not_broker_key(env, monkeypatch):
    import types as _types
    from channels.channel_handlers import _no_answer_response
    from verdict_broker import BROKER

    driver = env["driver"]
    eid = env["rec"].id

    # The broker key is GONE (retired + popped near tombstone retirement) — the
    # waiter MUST NOT consult BROKER.get_meta. Blow up if it does.
    def _boom(*a, **k):
        raise AssertionError("get_meta must not be called after the await (F2)")

    monkeypatch.setattr(BROKER, "get_meta", _boom)

    # The waiter holds its OWN req with the ORIGINAL inbound_gen (0).
    req = _types.SimpleNamespace(meta={"inbound_gen": 0})
    resp = await _no_answer_response(driver, eid, "rid-gone", req)

    body = _body(resp)
    assert body["engagement_paused"] is True
    # Away entry fired from req.meta gen 0 (== current spool gen 0).
    assert driver.operator_away_active(eid) is True


# ---------------------------------------------------------------------------
# Finding 3: a successful note_operator_away CAS drives the paused summary
# DIRECTLY (the timeout settle edit may be unconfirmed → recompute never runs).
# ---------------------------------------------------------------------------


async def test_anchor_away_retry_returns_recorded_outcome_not_unread(env):
    """Sol A2 wave-2, Finding 4: the anchor path now runs the reattach-outcome
    check BEFORE the unread-inbound gate (parity with the button path). An
    away-refused anchor whose operator then returns (inbound lands, away cleared
    but the message still UNREAD) must, on a same-request_id retry, return its
    RECORDED ``operator_away`` outcome — not ``unread_inbound``."""
    eid = env["rec"].id
    driver = env["driver"]
    driver._operator_away[eid] = True

    # First anchor ask is refused operator_away and records the outcome on the
    # intent (created_intent True → _record_intent_refusal).
    resp1 = await env["ask"](_FakeRequest(
        _payload(eid, request_id="a4", options=[])))
    assert _body(resp1)["error"] == "operator_away"

    # The operator returns: an inbound lands (clears away) but is still UNREAD
    # until the next turn-start consumes it.
    await driver.spool.enqueue("operator is back")
    driver._operator_away.pop(eid, None)
    assert driver.inbound_unread_depth(eid) > 0
    assert driver.operator_away_active(eid) is False

    # Same-request_id transport retry: reattach-first returns the RECORDED
    # operator_away outcome, NOT unread_inbound (the pre-fix ordering bug).
    resp2 = await env["ask"](_FakeRequest(
        _payload(eid, request_id="a4", options=[])))
    body2 = _body(resp2)
    assert body2["ok"] is False
    assert body2["error"] == "operator_away"
    assert body2["error"] != "unread_inbound"
    # Still no anchor posted, no broker request across both attempts.
    assert env["ch"].sent_texts == []
    assert env["broker"].pending(namespace="engagement_ask", scope=eid) == []


# ---------------------------------------------------------------------------
# Sol A2 wave-3, Finding 3: an UNREAD-inbound-refused ask records a refusal
# OUTCOME (refusal-count-free) on the intent — symmetric with the away-refusal
# fix — so a same-request_id transport retry reattaches and returns
# ``unread_inbound`` IMMEDIATELY, never awaiting the dead intent (→ the
# deferred-post budget → ``delivery_failed``).
# ---------------------------------------------------------------------------


async def test_button_unread_refusal_retry_short_circuits_no_delivery_failed(env):
    eid = env["rec"].id
    driver = env["driver"]
    # An unread operator message is pending → the inbound gate refuses.
    await driver.spool.enqueue("operator message")
    assert driver.inbound_unread_depth(eid) > 0

    resp1 = await env["ask"](_FakeRequest(_payload(eid, request_id="u1")))
    body1 = _body(resp1)
    assert body1["error"] == "unread_inbound"
    assert driver.ask_refusals == 1

    # Same-request_id transport retry: reattaches to the RECORDED refusal and
    # returns unread_inbound immediately — NOT delivery_failed.
    resp2 = await env["ask"](_FakeRequest(_payload(eid, request_id="u1")))
    body2 = _body(resp2)
    assert body2["ok"] is False
    assert body2["error"] == "unread_inbound"
    # The recorded outcome is refusal-count-free (a reattach must not re-bump).
    assert "refusal_count" not in body2
    assert driver.ask_refusals == 1
    assert env["broker"].pending(namespace="engagement_ask", scope=eid) == []
    assert env["ch"].options_keyboards == []


async def test_anchor_unread_refusal_retry_short_circuits_no_delivery_failed(env):
    eid = env["rec"].id
    driver = env["driver"]
    await driver.spool.enqueue("operator message")
    assert driver.inbound_unread_depth(eid) > 0

    resp1 = await env["ask"](_FakeRequest(
        _payload(eid, request_id="u2", options=[])))
    assert _body(resp1)["error"] == "unread_inbound"

    resp2 = await env["ask"](_FakeRequest(
        _payload(eid, request_id="u2", options=[])))
    body2 = _body(resp2)
    assert body2["ok"] is False
    assert body2["error"] == "unread_inbound"       # NOT delivery_failed
    assert "refusal_count" not in body2
    assert env["ch"].sent_texts == []
    assert env["broker"].pending(namespace="engagement_ask", scope=eid) == []


async def test_note_operator_away_drives_paused_summary_directly(tmp_path):
    from drivers.summary_controller import STATUS_PAUSED, STATUS_WAITING_REPLY
    drv, rec, ctrl, edits, reg = await _driver_with_summary(tmp_path)
    eid = rec.id

    # Establish a non-paused baseline (the ⏳ waiting an ordinary ask would set).
    await drv._summary_status_transition(eid, STATUS_WAITING_REPLY)
    assert ctrl._status == STATUS_WAITING_REPLY

    # No inbound spool ⇒ inbound_generation == 0; the CAS at gen 0 succeeds and
    # the entry submits STATUS_PAUSED through the funnel — regardless of whether
    # the keyboard settle edit ever confirmed.
    assert await drv.note_operator_away(eid, 0) is True
    assert ctrl._status == STATUS_PAUSED
    assert edits[-1][1].startswith(STATUS_PAUSED)
    ctrl.shutdown()
