"""v0.79.0 §2/§4 (review M2) — relay-mediated deferred posting preserves the
CAUSAL ORDER of narration vs. an ask keyboard / reply within one frame.

The bug this pins (C1): the ask/reply handler used to POST EAGERLY the moment the
MCP call arrived, ~500ms BEFORE the relay had posted the narration text block that
precedes it in the same assistant frame — so the keyboard landed ABOVE its own
preamble (lower message id). The fix defers the discrete post to the relay, which
posts it at the tool_use block position (after the preceding text block).

These are integration tests: the REAL ``OutputSequencer`` is shared between the
ask/reply handler (the discrete ingress) and a relay that processes one frame's
blocks in order (narration text → tool_use). Message ids come from ONE shared
counter, so ``narration_mid < keyboard_mid`` is a faithful ordering assertion.
They FAIL against the eager model (keyboard posts first → lower id) and PASS once
posting is relay-deferred.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

import agent as agent_mod
import verdict_broker
from verdict_broker import VerdictBroker
from channels.output_sequencer import (
    ASK_TOOL,
    REPLY_TOOL,
    OutputSequencer,
    projection_hash,
)

pytestmark = pytest.mark.asyncio


class _SharedIdChannel:
    """Telegram fake whose keyboard/reply posts AND the sequencer's narration
    posts all draw from ONE monotonic id counter, so post order == id order."""

    def __init__(self) -> None:
        self._next = 100
        self.keyboards: list[tuple[int, str]] = []
        self.replies: list[tuple[int, str]] = []
        self.narrations: list[tuple[int, str]] = []
        self.edits: list[dict] = []

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

    async def send_response_to_topic(self, topic_id, text) -> int:
        m = self._id()
        self.replies.append((m, text))
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
    chan = _SharedIdChannel()
    seq = OutputSequencer(
        engagement_id=rec.id, topic_id=42,
        send_message=chan.narration_send, edit_message=chan.narration_edit)
    drv = ClaudeCodeDriver(
        engagements_root=str(tmp_path / "eng"),
        send_to_topic=AsyncMock(), casa_framework_mcp_url="x", registry=reg)
    drv._sequencers[rec.id] = seq
    monkeypatch.setattr(agent_mod, "active_claude_code_driver", drv)
    handlers = _make_channel_handlers(
        telegram_channel=chan, engagement_registry=reg)
    return {
        "reg": reg, "rec": rec, "chan": chan, "seq": seq, "drv": drv,
        "broker": fresh_broker, "ask": handlers["/internal/channel/ask"],
        "send": handlers["/internal/channel/send_to_topic"],
    }


async def test_ask_keyboard_posts_after_preceding_narration(wired):
    """§2: narration text block precedes the ask tool_use block in one frame; the
    keyboard must post AFTER the narration (higher message id), not before it."""
    eid = wired["rec"].id
    seq, chan = wired["seq"], wired["chan"]
    h = projection_hash(
        ASK_TOOL, {"question": "Proceed?", "options": ["A", "B"], "timeout_s": 60})

    task = asyncio.ensure_future(wired["ask"](_FakeRequest({
        "engagement_id": eid, "request_id": "a1", "question": "Proceed?",
        "options": ["A", "B"], "timeout_s": 60, "projection_hash": h})))
    # Let the handler reach its post/await point (eager model posts HERE).
    await asyncio.sleep(0.02)

    # The relay now processes the frame's blocks IN ORDER: narration text first,
    # then the ask tool_use block.
    nar_mid = await seq.open_narration("Here is what I found. ")
    await seq.post_for_block(ASK_TOOL, h)

    # Operator taps.
    assert wired["broker"].deliver(
        namespace="engagement_ask", scope=eid, request_id="a1",
        option_index=0, actor_id=555) == "delivered"
    resp = await asyncio.wait_for(task, timeout=1.0)
    await wired["broker"].drain_hooks()

    assert _body(resp)["outcome"] == "answered"
    assert chan.keyboards, "keyboard was never posted"
    kb_mid = chan.keyboards[-1][0]
    # The whole point: narration is causally first → lower id → posted first.
    assert nar_mid < kb_mid
    assert chan.narrations[0][0] < chan.keyboards[0][0]


async def test_button_ask_retry_reattaches_no_second_keyboard(wired):
    """F2 (was N1): a same-request_id retry arriving while the first ask intent
    is still armed-but-NOT-posted must REATTACH — NO new Q-number, NO second
    keyboard, NO eager fallback. The probe showed Q2 posting before the relay's
    Q1 with both ledger entries surviving; here exactly one keyboard + one open
    question survive and both handlers resolve to the same answer."""
    eid = wired["rec"].id
    seq, reg = wired["seq"], wired["reg"]
    h = projection_hash(
        ASK_TOOL, {"question": "Proceed?", "options": ["A", "B"], "timeout_s": 60})
    payload = {
        "engagement_id": eid, "request_id": "dup", "question": "Proceed?",
        "options": ["A", "B"], "timeout_s": 60, "projection_hash": h}

    # First attempt: registers + arms the intent, then parks awaiting the tap
    # WITHOUT the relay having reached the block yet (no post_for_block).
    t1 = asyncio.ensure_future(wired["ask"](_FakeRequest(dict(payload))))
    await asyncio.sleep(0.02)
    # Retry (same request_id) BEFORE the relay posted → must reattach.
    t2 = asyncio.ensure_future(wired["ask"](_FakeRequest(dict(payload))))
    await asyncio.sleep(0.02)

    # Now the relay reaches the ask block → the keyboard posts exactly ONCE.
    await seq.post_for_block(ASK_TOOL, h)
    assert wired["broker"].deliver(
        namespace="engagement_ask", scope=eid, request_id="dup",
        option_index=0, actor_id=555) == "delivered"
    r1 = await asyncio.wait_for(t1, timeout=1.0)
    r2 = await asyncio.wait_for(t2, timeout=1.0)
    await wired["broker"].drain_hooks()

    assert _body(r1)["outcome"] == "answered"
    assert _body(r2)["outcome"] == "answered"
    # Exactly ONE keyboard, ONE open question — no Q2, no second post.
    assert len(wired["chan"].keyboards) == 1
    assert reg.open_question_numbers(eid) in ([], [1])   # settled or single Q1
    assert reg.get(eid).next_question_number == 2         # allocated once only


async def test_reply_posts_after_preceding_narration(wired):
    """§2 reply-ingress variant: a reply within a frame posts at its tool_use
    block position, AFTER the preceding narration text."""
    eid = wired["rec"].id
    seq, chan = wired["seq"], wired["chan"]
    h = projection_hash(REPLY_TOOL, {"text": "done — shipped it"})

    task = asyncio.ensure_future(wired["send"](_FakeRequest({
        "engagement_id": eid, "text": "done — shipped it",
        "request_id": "r1", "projection_hash": h})))
    await asyncio.sleep(0.02)

    nar_mid = await seq.open_narration("Wrapping up. ")
    await seq.post_for_block(REPLY_TOOL, h)
    await asyncio.wait_for(task, timeout=1.0)
    # let any deferred relay post settle
    await asyncio.sleep(0.02)

    assert chan.replies, "reply was never posted"
    reply_mid = chan.replies[-1][0]
    assert nar_mid < reply_mid
    assert chan.narrations[0][0] < chan.replies[0][0]
