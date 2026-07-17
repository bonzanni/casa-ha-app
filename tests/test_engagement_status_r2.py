"""W-R2 (v0.81.0) — status-correct summary layout.

A successful ask/anchor POST transitions the summary to ⏳ waiting for your
reply, driven from the ask LIFECYCLE (not the turn ``result``); settlement
RECOMPUTES from the remaining open questions (still waiting while any is open,
back to ⚙️ working only when none remain AND the turn is still running). A
terminal status is absolute. The v0.79 §5 revision allocator totally-orders the
transitions, and the ``_ask_registered`` linearization pin guarantees a fast tap
during the post window can never leave the summary stuck-waiting.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

pytestmark = pytest.mark.asyncio

from drivers.summary_controller import (  # noqa: E402
    STATUS_COMPLETED,
    STATUS_WAITING_REPLY,
    STATUS_WORKING,
    SummaryController,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSequencer:
    def __init__(self) -> None:
        self.edits: list[tuple[int, str]] = []

    @asynccontextmanager
    async def serialized(self):
        # GLOBAL LOCK-ORDER: controller ``_writing`` takes this OUTER; no-op here.
        yield

    async def edit_summary(self, msg_id: int, text: str) -> str:
        from channels.output_sequencer import APPLIED
        self.edits.append((msg_id, text))
        return APPLIED


class _RevReg:
    """Minimal registry stub: a monotonic revision allocator + an open-question
    ledger the recompute reads."""

    def __init__(self) -> None:
        self._rev = 0
        self.open: list[int] = []

    async def allocate_summary_revision(self, eid: str) -> int:
        r = self._rev
        self._rev += 1
        return r

    def open_question_numbers(self, eid: str) -> list[int]:
        return list(self.open)


def _driver_with_summary(reg, *, turn_running=True):
    from unittest.mock import AsyncMock
    from drivers.claude_code_driver import ClaudeCodeDriver

    drv = ClaudeCodeDriver(
        engagements_root="/tmp/does-not-matter",
        send_to_topic=AsyncMock(),
        casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        registry=reg,
    )
    eid = "eng-r2"
    ctrl = SummaryController(
        engagement_id=eid,
        sequencer=_FakeSequencer(),
        goal_line="Gmail plugin",
        open_question_numbers=lambda: list(reg.open),
        message_id=500,
    )
    drv._summaries[eid] = ctrl
    drv._turn_running[eid] = turn_running
    return drv, ctrl, eid


# ---------------------------------------------------------------------------
# note_ask_waiting + recompute_engagement_status
# ---------------------------------------------------------------------------


async def test_ask_post_transitions_to_waiting():
    reg = _RevReg()
    drv, ctrl, eid = _driver_with_summary(reg)
    # Turn running → ⚙️ working initially.
    await drv._summary_status_transition(eid, STATUS_WORKING)
    assert ctrl._status == STATUS_WORKING
    # A posted ask hands the ball to the operator.
    reg.open = [11]
    await drv.note_ask_waiting(eid)
    assert ctrl._status == STATUS_WAITING_REPLY
    ctrl.shutdown()


async def test_settle_with_remaining_stays_waiting():
    reg = _RevReg()
    drv, ctrl, eid = _driver_with_summary(reg)
    reg.open = [11, 12]
    await drv.note_ask_waiting(eid)
    assert ctrl._status == STATUS_WAITING_REPLY
    # Q11 answered but Q12 still open → still waiting.
    reg.open = [12]
    await drv.recompute_engagement_status(eid)
    assert ctrl._status == STATUS_WAITING_REPLY
    ctrl.shutdown()


async def test_settle_last_returns_to_working_when_turn_active():
    reg = _RevReg()
    drv, ctrl, eid = _driver_with_summary(reg, turn_running=True)
    reg.open = [11]
    await drv.note_ask_waiting(eid)
    assert ctrl._status == STATUS_WAITING_REPLY
    # Last question settled + the turn is still running → back to working.
    reg.open = []
    await drv.recompute_engagement_status(eid)
    assert ctrl._status == STATUS_WORKING
    ctrl.shutdown()


async def test_settle_last_stays_waiting_when_turn_not_running():
    reg = _RevReg()
    drv, ctrl, eid = _driver_with_summary(reg, turn_running=False)
    reg.open = [11]
    await drv.note_ask_waiting(eid)
    reg.open = []
    # No live turn → recompute does NOT force working (turn-result path owns that).
    await drv.recompute_engagement_status(eid)
    assert ctrl._status == STATUS_WAITING_REPLY
    ctrl.shutdown()


async def test_terminal_status_overrides_and_no_approval_wired():
    reg = _RevReg()
    drv, ctrl, eid = _driver_with_summary(reg)
    reg.open = [11]
    await drv.note_ask_waiting(eid)
    await drv.finalize_summary(_rec(eid), "completed")
    assert ctrl._status == STATUS_COMPLETED
    # Terminal is absolute: a later waiting/working recompute cannot override.
    reg.open = []
    await drv.recompute_engagement_status(eid)
    assert ctrl._status == STATUS_COMPLETED
    # No approval-status wiring in v0.81 — the ask lifecycle only ever submits
    # working/waiting, never 🔐 waiting for your approval.
    from drivers.summary_controller import STATUS_WAITING_APPROVAL
    all_texts = "".join(t for _, t in ctrl._sequencer.edits)
    assert STATUS_WAITING_APPROVAL not in all_texts


def _rec(eid):
    from engagement_registry import EngagementRecord
    return EngagementRecord(
        id=eid, kind="executor", role_or_type="configurator",
        driver="claude_code", status="active", topic_id=1,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )


# ---------------------------------------------------------------------------
# Linearization pin (Sol r2-1): a fast tap during the post/registration window.
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self) -> None:
        self._next_id = 9000
        self.edits: list = []

    async def post_options_keyboard(self, *, engagement_id, request_id,
                                    question, options):
        mid = self._next_id
        self._next_id += 1
        return mid

    async def edit_topic_message(self, topic_id, message_id, text, *,
                                 clear_keyboard=False):
        self.edits.append((message_id, text, clear_keyboard))
        return True


class _StatusDriver:
    """A driver stub for the ask handler that records status transitions and
    exposes the real registry seams the linearization pin exercises."""

    def __init__(self, reg, eid) -> None:
        from channels.output_sequencer import OutputSequencer

        self._reg = reg
        self._eid = eid
        self.status_calls: list[str] = []
        self.depth = 0
        self.gen = 0
        self._relay_tasks: list = []

        async def _noop_send(topic, text, reply_to=None):
            return None

        async def _noop_edit(topic, mid, text):
            return True

        self.seq = OutputSequencer(
            engagement_id=eid, topic_id=42,
            send_message=_noop_send, edit_message=_noop_edit)

    # inbound gate reads
    def inbound_unread_depth(self, eid):
        return self.depth

    def inbound_generation(self, eid):
        return self.gen

    def record_ask_refusal(self, eid):
        return 1

    # discrete-intent seam → real sequencer registry
    def register_send_intent(self, *, engagement_id, request_id, tool_name,
                             projection_hash, poster, on_retire=None):
        return self.seq.register_intent(
            request_id=request_id, tool_name=tool_name,
            projection_hash=projection_hash, poster=poster, on_retire=on_retire)

    def set_send_intent_poster(self, eid, rid, poster):
        return self.seq.set_intent_poster(rid, poster)

    def arm_send_intent(self, eid, rid):
        intent = self.seq.arm_intent(rid)
        if intent is not None:
            self._relay_tasks.append(asyncio.ensure_future(
                self.seq.post_for_block(intent.tool_name,
                                        intent.projection_hash)))
        return intent

    def cancel_send_intent(self, eid, rid):
        return self.seq.cancel_intent(rid)

    def send_intent_outcome(self, eid, rid):
        return self.seq.intent_outcome(rid)

    async def await_send_intent(self, eid, rid, timeout=None):
        if self._relay_tasks:
            await asyncio.gather(*self._relay_tasks, return_exceptions=True)
            self._relay_tasks.clear()
        return await self.seq.await_intent_resolution(rid, timeout)

    # W-R2 status seams (recorded, backed by the real recompute logic)
    async def note_ask_waiting(self, eid):
        self.status_calls.append(STATUS_WAITING_REPLY)

    async def recompute_engagement_status(self, eid):
        open_qs = self._reg.open_question_numbers(eid)
        self.status_calls.append(
            STATUS_WAITING_REPLY if open_qs else STATUS_WORKING)


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


async def test_fast_tap_during_registration_never_stuck_waiting(
    tmp_path, monkeypatch,
):
    """The finish hook can become runnable (a fast tap) BEFORE the post path
    finishes registering the open question + submitting waiting. The
    ``_ask_registered`` pin forces settlement to run AFTER registration+waiting,
    so the final recorded status order is waiting → working (never stuck)."""
    import agent as agent_mod
    import verdict_broker
    from verdict_broker import VerdictBroker
    from engagement_registry import EngagementRegistry
    from channels.channel_handlers import _make_channel_handlers

    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 555}, topic_id=42)
    eid = rec.id

    ch = _FakeChannel()
    driver = _StatusDriver(reg, eid)
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", driver)

    handlers = _make_channel_handlers(
        telegram_channel=ch, engagement_registry=reg)
    ask = handlers["/internal/channel/ask"]

    payload = {
        "engagement_id": eid, "request_id": "fast-1",
        "question": "Proceed?", "options": ["A", "B"], "timeout_s": 60,
        "projection_hash": "hash-abc",
    }
    task = asyncio.ensure_future(ask(_FakeRequest(payload)))
    # Deliver a tap ASAP — racing the registration window.
    await asyncio.sleep(0)
    for _ in range(50):
        if fresh.deliver(namespace="engagement_ask", scope=eid,
                         request_id="fast-1", option_index=0,
                         actor_id=555) == "delivered":
            break
        await asyncio.sleep(0.005)
    resp = await asyncio.wait_for(task, timeout=2.0)
    await fresh.drain_hooks()
    assert json.loads(resp.text)["ok"] is True

    # The question settled → ledger empty.
    assert reg.open_question_numbers(eid) == []
    # The pin guarantees ordering: waiting was submitted BEFORE the settlement
    # recompute, and the LAST status is working (no open questions remain) —
    # never stuck-waiting.
    assert driver.status_calls[0] == STATUS_WAITING_REPLY
    assert driver.status_calls[-1] == STATUS_WORKING
