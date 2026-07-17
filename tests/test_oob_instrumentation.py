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

import pytest
from aiohttp import web

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker
from channels.output_sequencer import ASK_TOOL, OutputSequencer

pytestmark = pytest.mark.asyncio

_ANCHOR_HASH = "anchor-hash"


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
