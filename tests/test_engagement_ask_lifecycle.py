"""v0.79.0 (§4) — ask lifecycle: settle-edits, inbound gate, supersession,
numbered free-text anchors, canonical Q-numbers, reply reattachment.

Drives the ``/internal/channel/ask`` + ``/internal/channel/send_to_topic``
handlers directly with a minimal ``_FakeRequest``, a REAL ``EngagementRegistry``
(so durable numbering is exercised) and a fake ``claude_code`` driver injected
via ``agent.active_claude_code_driver`` (the same seam production uses).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

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


class _FakeDriver:
    """Fake claude_code driver backed by a REAL ``OutputSequencer`` (§2).

    The discrete-intent seam delegates to the real registry so the deferred
    relay-mediated posting model (review C1) is exercised end-to-end. Since
    these tests have no live topic-stream relay, ``arm_send_intent`` SIMULATES
    the relay reaching the intent's tool_use block right after arm — it
    schedules ``post_for_block`` on the real sequencer, which invokes the
    handler-installed poster (posts the keyboard/anchor/reply and records the
    outcome), exactly as the production relay would.
    """

    def __init__(self, engagement_id: str = "e", topic_id: int = 42) -> None:
        from channels.output_sequencer import OutputSequencer

        self.depth = 0
        self.gen = 0
        self.refusals = 0
        self._relay_tasks: list = []

        async def _noop_send(topic, text, reply_to=None):
            return None

        async def _noop_edit(topic, mid, text):
            return True

        self.seq = OutputSequencer(
            engagement_id=engagement_id, topic_id=topic_id,
            send_message=_noop_send, edit_message=_noop_edit)

    # inbound gate reads
    def inbound_unread_depth(self, eid) -> int:
        return self.depth

    def inbound_generation(self, eid) -> int:
        return self.gen

    def record_ask_refusal(self, eid) -> int:
        self.refusals += 1
        return self.refusals

    # discrete-intent seam (delegates to the real sequencer registry)
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
            # Simulate the relay reaching this block just after arm.
            self._relay_tasks.append(asyncio.ensure_future(
                self.seq.post_for_block(intent.tool_name, intent.projection_hash)))
        return intent

    def cancel_send_intent(self, eid, rid):
        return self.seq.cancel_intent(rid)

    def send_intent_outcome(self, eid, rid):
        return self.seq.intent_outcome(rid)

    async def mark_send_intent_posted(self, eid, rid, mid):
        return await self.seq.mark_intent_posted(rid, mid)

    async def await_send_intent(self, eid, rid, timeout=None):
        # F3: let the simulated relay tasks (scheduled on arm) run, then return
        # the recorded outcome so the handler returns ok only when the post
        # landed.
        if self._relay_tasks:
            await asyncio.gather(*self._relay_tasks, return_exceptions=True)
            self._relay_tasks.clear()
        return await self.seq.await_intent_resolution(rid, timeout)

    def intent_state(self, rid):
        intent = self.seq.registry.by_request_id(rid)
        return intent.state if intent is not None else None


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
    driver = _FakeDriver()
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)

    handlers = _make_channel_handlers(telegram_channel=ch, engagement_registry=reg)
    ask = handlers["/internal/channel/ask"]
    send = handlers["/internal/channel/send_to_topic"]
    return {
        "reg": reg, "rec": rec, "ch": ch, "driver": driver,
        "broker": fresh_broker, "ask": ask, "send": send,
    }


def _ask_payload(**over) -> dict:
    base = {
        "engagement_id": "PLACEHOLDER", "request_id": "rid-1",
        "question": "Proceed?", "options": ["A", "B"], "timeout_s": 60,
        "projection_hash": "hash-abc",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Settle-edit copies (clear_keyboard) + canonical Q-number
# ---------------------------------------------------------------------------


async def test_answered_settles_with_check_and_clears_keyboard(env):
    eid = env["rec"].id
    task = asyncio.ensure_future(env["ask"](
        _FakeRequest(_ask_payload(engagement_id=eid, request_id="a1"))))
    await asyncio.sleep(0.02)
    assert env["broker"].deliver(
        namespace="engagement_ask", scope=eid, request_id="a1",
        option_index=1, actor_id=555) == "delivered"
    resp = await asyncio.wait_for(task, timeout=1.0)
    await env["broker"].drain_hooks()

    assert _body(resp) == {
        "ok": True, "outcome": "answered", "option": "B", "option_index": 1}
    # Settle edit: PRESENT clear_keyboard, ✅ + FULL chosen option appended
    # BELOW the canonical body (W-R3: body carries every option verbatim).
    edit = env["ch"].edits[-1]
    assert edit["clear_keyboard"] is True
    assert edit["text"] == "Q1: Proceed?\n\n1. A\n2. B\n✅ B"


async def test_expired_settles_with_hourglass_and_clears_keyboard(env, monkeypatch):
    import channels.channel_handlers  # noqa: F401
    eid = env["rec"].id
    # Fire the timeout immediately.
    task = asyncio.ensure_future(env["ask"](
        _FakeRequest(_ask_payload(engagement_id=eid, request_id="e1", timeout_s=30))))
    await asyncio.sleep(0.02)
    env["broker"]._on_timeout(("engagement_ask", eid, "e1"))
    resp = await asyncio.wait_for(task, timeout=1.0)
    await env["broker"].drain_hooks()

    assert _body(resp) == {"ok": True, "outcome": "no_answer"}
    edit = env["ch"].edits[-1]
    assert edit["clear_keyboard"] is True
    assert edit["text"] == (
        "Q1: Proceed?\n\n1. A\n2. B\n⌛ expired — answer by text below")


async def test_canonical_qnumber_strips_agent_authored_prefix(env):
    eid = env["rec"].id
    # Agent authored its own "Q7:" — must be stripped and re-prefixed with the
    # ALLOCATED durable number so message == registry == summary accessor.
    task = asyncio.ensure_future(env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, request_id="c1", question="Q7: Which DB?"))))
    await asyncio.sleep(0.02)
    posted_q = env["ch"].options_keyboards[-1]["question"]
    assert posted_q == "Q1: Which DB?\n\n1. A\n2. B"
    # open_questions ledger + summary accessor agree with the message.
    assert env["reg"].open_question_numbers(eid) == [1]
    env["broker"].deliver(
        namespace="engagement_ask", scope=eid, request_id="c1",
        option_index=0, actor_id=555)
    await asyncio.wait_for(task, timeout=1.0)
    await env["broker"].drain_hooks()
    # Settled → closed in the ledger.
    assert env["reg"].open_question_numbers(eid) == []


# ---------------------------------------------------------------------------
# Free-text anchor (options: [])
# ---------------------------------------------------------------------------


async def test_free_text_anchor_posts_numbered_and_registers(env):
    eid = env["rec"].id
    resp = await env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, request_id="ft1",
        question="What's the DB name?", options=[])))
    body = _body(resp)
    assert body["ok"] is True and body["outcome"] == "anchored"
    assert body["question_number"] == 1
    # Posting is RELAY-DEFERRED (§2, C1): the numbered anchor posts when the
    # relay reaches the ask tool_use block. Drive the (simulated) relay.
    await asyncio.sleep(0.01)
    # Posted as a plain numbered anchor (NO keyboard) + registered open.
    assert env["ch"].sent_texts[-1] == (42, "Q1: What's the DB name?")
    assert env["ch"].options_keyboards == []
    assert env["reg"].open_question_numbers(eid) == [1]


# ---------------------------------------------------------------------------
# Inbound gate + escalation
# ---------------------------------------------------------------------------


async def test_unread_inbound_refuses_without_registering(env):
    eid = env["rec"].id
    env["driver"].depth = 1  # operator message waiting, unseen
    resp = await env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, request_id="g1")))
    body = _body(resp)
    assert body["ok"] is False and body["error"] == "unread_inbound"
    assert "end your turn now" in body["message"]
    assert body["refusal_count"] == 1
    # No keyboard posted, no live broker request, intent tombstoned.
    assert env["ch"].options_keyboards == []
    assert env["broker"].pending(namespace="engagement_ask", scope=eid) == []
    assert env["driver"].intent_state("g1") == "cancelled"


async def test_free_text_anchor_also_gated_on_unread(env):
    eid = env["rec"].id
    env["driver"].depth = 1
    resp = await env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, request_id="ga1", question="DB name?", options=[])))
    body = _body(resp)
    assert body["ok"] is False and body["error"] == "unread_inbound"
    # No anchor posted, nothing registered.
    assert env["ch"].sent_texts == []
    assert env["reg"].open_question_numbers(eid) == []


async def test_refusal_escalates_at_third(env):
    eid = env["rec"].id
    env["driver"].depth = 1
    msgs = []
    for i in range(3):
        resp = await env["ask"](_FakeRequest(_ask_payload(
            engagement_id=eid, request_id=f"r{i}")))
        msgs.append(_body(resp)["message"])
    assert msgs[0] == msgs[1]
    assert msgs[2] != msgs[0]
    assert "STOP ASKING" in msgs[2]


# ---------------------------------------------------------------------------
# Generation re-check supersession
# ---------------------------------------------------------------------------


async def test_generation_recheck_supersedes(env):
    eid = env["rec"].id
    ch = env["ch"]
    driver = env["driver"]

    # An operator message lands in the register→post window: bump the
    # generation the moment the keyboard is posted.
    orig_post = ch.post_options_keyboard

    async def _post(*, engagement_id, request_id, question, options):
        driver.gen += 1  # operator envelope arrived during the post
        return await orig_post(
            engagement_id=engagement_id, request_id=request_id,
            question=question, options=options)

    ch.post_options_keyboard = _post

    resp = await env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, request_id="s1")))
    body = _body(resp)
    assert body["ok"] is False and body["error"] == "superseded"
    await env["broker"].drain_hooks()
    # Keyboard settled with the superseded copy + cleared.
    edit = ch.edits[-1]
    assert edit["clear_keyboard"] is True
    assert edit["text"] == (
        "Q1: Proceed?\n\n1. A\n2. B\n🚫 superseded by your message below")


# ---------------------------------------------------------------------------
# Reply reattachment (response-loss-after-post)
# ---------------------------------------------------------------------------


async def test_reply_retry_reattaches_no_double_post(env):
    eid = env["rec"].id
    p = {"engagement_id": eid, "text": "hello operator",
         "request_id": "rep-1", "projection_hash": "rh"}
    r1 = await env["send"](_FakeRequest(p))
    b1 = _body(r1)
    assert b1["ok"] is True  # armed; posting is relay-deferred (§2, C1)
    # Drive the (simulated) relay: the poster posts ONCE and records the outcome.
    await asyncio.sleep(0.01)
    assert env["ch"].sent_texts.count((42, "hello operator")) == 1
    first_mid = env["driver"].send_intent_outcome(eid, "rep-1")["message_id"]
    assert first_mid is not None

    # A transport retry with the SAME request_id must reattach — one post only.
    r2 = await env["send"](_FakeRequest(p))
    b2 = _body(r2)
    assert b2 == {"ok": True, "message_id": first_mid}
    assert env["ch"].sent_texts.count((42, "hello operator")) == 1


# ---------------------------------------------------------------------------
# F3 — fail-closed deferred posting (ok:true + outcome ok:false impossible)
# ---------------------------------------------------------------------------


class _FailingChannel(_FakeChannel):
    """A channel whose topic send FAILS (returns None) — models a transient
    Telegram failure of the relay-deferred reply/anchor post."""

    async def send_response_to_topic(self, topic_id, text) -> int | None:
        self.sent_texts.append((topic_id, text))
        return None


@pytest.fixture
async def failing_env(tmp_path, fresh_broker, monkeypatch):
    from engagement_registry import EngagementRegistry
    from channels.channel_handlers import _make_channel_handlers

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    ch = _FailingChannel()
    driver = _FakeDriver()
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)
    handlers = _make_channel_handlers(telegram_channel=ch, engagement_registry=reg)
    return {
        "reg": reg, "rec": rec, "ch": ch, "driver": driver,
        "broker": fresh_broker, "ask": handlers["/internal/channel/ask"],
        "send": handlers["/internal/channel/send_to_topic"],
    }


async def test_reply_poster_failure_returns_ok_false(failing_env):
    """F3: the deferred reply poster fails → the handler AWAITS the outcome and
    returns ok:false. An ok:true response with a failed post is impossible."""
    eid = failing_env["rec"].id
    r = await failing_env["send"](_FakeRequest({
        "engagement_id": eid, "text": "shipped it",
        "request_id": "rf-1", "projection_hash": "rh"}))
    assert _body(r)["ok"] is False
    # The intent recorded an ok:false outcome (surfaced, not swallowed).
    outcome = failing_env["driver"].send_intent_outcome(eid, "rf-1")
    assert outcome is not None and outcome["ok"] is False


async def test_anchor_poster_failure_returns_ok_false(failing_env):
    """F3: the free-text anchor poster fails → ok:false, never ok:true."""
    eid = failing_env["rec"].id
    r = await failing_env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, request_id="af-1",
        question="Which DB?", options=[])))
    assert _body(r)["ok"] is False


# ---------------------------------------------------------------------------
# F5 — deferred retry fail-closed + anchor no-double-number-allocation.
# ---------------------------------------------------------------------------


class _UnresolvedDriver:
    """A driver whose deferred intent is ARMED but the relay never posts — so a
    retry reattaches to an UNRESOLVED intent and ``await_send_intent`` times out
    to None. Models the F5 fail-open probe: the handler must NOT return ok:true
    on an unresolved intent."""

    def __init__(self, created: bool = False) -> None:
        self.depth = 0
        self.gen = 0
        self.refusals = 0
        self._created = created

    def inbound_unread_depth(self, eid) -> int:
        return self.depth

    def inbound_generation(self, eid) -> int:
        return self.gen

    def record_ask_refusal(self, eid) -> int:
        self.refusals += 1
        return self.refusals

    def register_send_intent(self, *, engagement_id, request_id, tool_name,
                             projection_hash, poster):
        return (object(), self._created)

    def set_send_intent_poster(self, eid, rid, poster):
        return None

    def arm_send_intent(self, eid, rid):
        return None

    def cancel_send_intent(self, eid, rid):
        return None

    def send_intent_outcome(self, eid, rid):
        return None  # unresolved: no recorded outcome

    async def await_send_intent(self, eid, rid, timeout=None):
        return None  # bounded resolution timed out


async def test_reply_retry_unresolved_awaits_and_fails_closed(env, monkeypatch):
    """F5: a reply RETRY reattaching to an unresolved intent AWAITS the same
    bounded resolution; a None/timeout maps to ok:false — never ok:true with no
    post."""
    driver = _UnresolvedDriver(created=False)
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)
    eid = env["rec"].id
    r = await env["send"](_FakeRequest({
        "engagement_id": eid, "text": "hi", "request_id": "rep-u",
        "projection_hash": "rh"}))
    b = _body(r)
    assert b["ok"] is False and b["error"] == "send_failed"
    assert env["ch"].sent_texts == []  # nothing posted


async def test_reply_first_attempt_unresolved_fails_closed(env, monkeypatch):
    """F5: even the FIRST attempt fails closed when the post never resolves
    (outcome None) — the old code returned ok:true with message_id None."""
    driver = _UnresolvedDriver(created=True)
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)
    eid = env["rec"].id
    r = await env["send"](_FakeRequest({
        "engagement_id": eid, "text": "hi", "request_id": "rep-f",
        "projection_hash": "rh"}))
    assert _body(r)["ok"] is False


async def test_anchor_retry_unresolved_fails_closed_no_number(env, monkeypatch):
    """F5: a free-text anchor RETRY reattaching to an unresolved intent fails
    closed AND does not burn a fresh Q-number (reattach check precedes number
    allocation)."""
    driver = _UnresolvedDriver(created=False)
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)
    eid = env["rec"].id
    before = env["reg"].get(eid).next_question_number
    r = await env["ask"](_FakeRequest(_ask_payload(
        engagement_id=eid, request_id="fu", question="DB?", options=[])))
    b = _body(r)
    assert b["ok"] is False and b["error"] == "delivery_failed"
    assert env["reg"].get(eid).next_question_number == before  # no number burned
    assert env["ch"].sent_texts == []


async def test_anchor_retry_reattaches_without_new_qnumber(env):
    """F5: a free-text anchor transport RETRY reattaches — NO new Q-number, NO
    second anchor, single open-question entry (parity with the button reattach).
    """
    eid = env["rec"].id
    p = _ask_payload(engagement_id=eid, request_id="ftr",
                     question="DB?", options=[])
    r1 = await env["ask"](_FakeRequest(p))
    assert _body(r1)["question_number"] == 1
    await asyncio.sleep(0.01)
    assert env["reg"].get(eid).next_question_number == 2  # 1 allocated → next 2
    sent_before = list(env["ch"].sent_texts)

    r2 = await env["ask"](_FakeRequest(p))
    b2 = _body(r2)
    assert b2["ok"] is True and b2["outcome"] == "anchored"
    # No fresh number allocated on the reattach (old code allocated at line 577
    # BEFORE the reattach check → next would advance to 3):
    assert env["reg"].get(eid).next_question_number == 2
    assert env["ch"].sent_texts == sent_before          # no second anchor
    assert env["reg"].open_question_numbers(eid) == [1]  # single open question


# ---------------------------------------------------------------------------
# F1 (Sol r3) — button-ask reattach/creation race must not lose static metadata
# ---------------------------------------------------------------------------


async def test_button_ask_reattach_race_preserves_static_metadata(env, monkeypatch):
    """F1: a button-ask transport RETRY that CREATES the broker request while
    the first attempt is suspended in number allocation must not strip the
    keyboard's static metadata.

    Regression (Sol r3 re-probe): the reattach path created the broker request
    WITHOUT options/topic_id/operator_id; the first attempt then resumed, saw
    ``created=False`` and skipped the meta init, leaving broker meta =
    {"message_id": ...} only ⇒ every tap rejected.
    """
    eid = env["rec"].id
    reg = env["reg"]

    parked = asyncio.Event()   # set once the first attempt is inside allocation
    resume = asyncio.Event()   # released after the retry has registered
    orig_alloc = reg.allocate_question_number
    calls = {"n": 0}

    async def _slow_alloc(engagement_id):
        calls["n"] += 1
        if calls["n"] == 1:
            # First attempt suspends INSIDE allocation; hand off to the retry.
            parked.set()
            await resume.wait()
        return await orig_alloc(engagement_id)

    monkeypatch.setattr(reg, "allocate_question_number", _slow_alloc)

    p = _ask_payload(engagement_id=eid, request_id="race-1")
    first = asyncio.ensure_future(env["ask"](_FakeRequest(p)))
    await asyncio.wait_for(parked.wait(), timeout=1.0)

    # The RETRY (same request_id) reattaches and, in the race, CREATES the
    # broker request. It must seed the complete static metadata.
    second = asyncio.ensure_future(env["ask"](_FakeRequest(p)))
    await asyncio.sleep(0.02)   # let the retry register the broker request

    # Let the first attempt resume: its main-path register now sees created=False.
    resume.set()
    await asyncio.sleep(0.02)

    meta = env["broker"].get_meta(
        namespace="engagement_ask", scope=eid, request_id="race-1")
    assert meta is not None
    assert meta.get("topic_id") == 42
    assert meta.get("operator_id") == 555
    assert meta.get("options") == ["A", "B"]

    # A tap is accepted — it resolves the ask to "answered" (the meta the
    # inline-callback handler validates against — topic_id/operator_id/options —
    # is all present, so the tap is not rejected).
    assert env["broker"].deliver(
        namespace="engagement_ask", scope=eid, request_id="race-1",
        option_index=0, actor_id=555) == "delivered"
    resp = await asyncio.wait_for(first, timeout=1.0)
    assert _body(resp)["outcome"] == "answered"
    await asyncio.gather(second, return_exceptions=True)
    await env["broker"].drain_hooks()


# ---------------------------------------------------------------------------
# W-R1 (Sol r2-2): confirmed-edit settle gating on the finish hook
# ---------------------------------------------------------------------------


class _FailEditChannel:
    """Channel whose settle edit is transiently failing (returns False, as
    ``edit_topic_message`` does on a timeout / non-'not-modified' BadRequest)."""

    def __init__(self, succeed_on: int | None = None) -> None:
        self.attempts = 0
        self.succeed_on = succeed_on  # 1-based attempt that starts succeeding
        self.edits: list[dict] = []

    async def edit_topic_message(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        self.attempts += 1
        ok = self.succeed_on is not None and self.attempts >= self.succeed_on
        self.edits.append(
            {"message_id": message_id, "text": text,
             "clear_keyboard": clear_keyboard, "ok": ok})
        return ok


async def test_finish_hook_preserves_ledger_on_unconfirmed_edit():
    """R1: an all-failing settle edit → the finish hook retries EXACTLY 3× with
    0.5s→1s→2s backoff (injected clock) and DOES NOT close the ledger entry."""
    from channels.channel_handlers import _ask_keyboard_finish

    ch = _FailEditChannel(succeed_on=None)  # never succeeds
    sleeps: list[float] = []

    async def _sleep(d):
        sleeps.append(d)

    closed = {"n": 0}

    async def _on_settle():
        closed["n"] += 1

    hook = _ask_keyboard_finish(
        ch, 42, 101, "Q1: Proceed?\n\n1. A\n2. B", ["A", "B"],
        on_settle=_on_settle, sleep=_sleep)
    await hook({"outcome": "answered", "option_index": 0})

    assert ch.attempts == 3           # exactly 3 bounded attempts
    assert sleeps == [0.5, 1.0, 2.0]  # 0.5→1→2 backoff via injected clock
    assert closed["n"] == 0           # ledger entry PRESERVED (no premature close)


async def test_finish_hook_closes_once_when_edit_confirmed_on_retry_two():
    """R1: edit succeeds on the SECOND attempt → ledger closed exactly once."""
    from channels.channel_handlers import _ask_keyboard_finish

    ch = _FailEditChannel(succeed_on=2)  # fail 1, confirm on 2
    sleeps: list[float] = []

    async def _sleep(d):
        sleeps.append(d)

    closed = {"n": 0}

    async def _on_settle():
        closed["n"] += 1

    hook = _ask_keyboard_finish(
        ch, 42, 101, "Q1: Proceed?\n\n1. A\n2. B", ["A", "B"],
        on_settle=_on_settle, sleep=_sleep)
    await hook({"outcome": "answered", "option_index": 1})

    assert ch.attempts == 2
    assert sleeps == [0.5]   # slept once after the first failure, then confirmed
    assert closed["n"] == 1  # ledger closed exactly once
