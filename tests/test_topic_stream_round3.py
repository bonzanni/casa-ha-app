"""Round-3 F-DUP tests for ``drivers.topic_stream`` — the COLD-path half of the
A1 fix (design §A1, spec ``2026-07-15-engagement-ux-round3-design.md``).

Task 1 (A1a) makes ``cursor.last_posted_len`` load-bearing for the FIRST time:
the delta-aware cold ``_reconcile`` reposts only the genuinely-unposted tail
past the PERSISTED wire high-water, and every checkpoint site persists the WIRE
truth (``_posted_len``) rather than the in-memory ``len(_per_message_text)``
(which can include a throttled, never-posted suffix).

Each test drives a REAL ``OutputSequencer`` (constructed inside ``TopicStreamRelay``
with fake async send/edit recorders that return incrementing message ids) over a
REAL temp NDJSON log dir — never a mock of the sequencer. Time is injected
(``edit_throttle`` + a no-op ``_sleep``); we never patch ``asyncio.sleep``.
"""
from __future__ import annotations

import os

from drivers.topic_stream import StreamCursor
from test_topic_stream import (
    Recorder,
    _ident,
    _init,
    _make_relay,
    _reply_tool_frame,
    _text,
    _tool,
    _write_current,
)

pytestmark = __import__("pytest").mark.asyncio


# ---------------------------------------------------------------------------
# spec §A1 RED test 4 — delta-aware cold reconcile posts only the unposted tail.
# ---------------------------------------------------------------------------


async def test_cold_reconcile_sealed_posts_only_unposted_tail(tmp_path):
    """§A1(2): a FRESH relay object recovers an OPEN turn whose narration was
    sealed by a discrete post before a crash. The persisted ``last_posted_len``
    records that only the PREFIX reached the wire (a lost-before-persist edit
    carried the rest). The delta-aware reconcile must repost ONLY the genuinely-
    unposted tail as a NEW message — never the already-visible prefix (the
    production F-DUP reposted the whole tail) — and adopt the delta message
    exactly like ``_execute_ops``' sealed branch.

    Log: init → "hello " → "world" (together "hello world") → a discrete reply
    frame (sealed narration live). Crash-before-persist: only "hello " (6 chars)
    was confirmed on the wire; the edit to "hello world" landed on Telegram but
    the cursor save was lost.
    """
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("hello "), _text("world"), _reply_tool_frame("R"),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},  # everything replayed
        message_ids=[7],
        last_posted_len=len("hello "),  # only the prefix reached the wire
    ).save(cur_path)

    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    # ONLY the unposted tail "world" is reposted — never the visible "hello ".
    assert rec.sends == [(42, "world")]
    assert all(mid != 7 for _t, mid, _x in rec.edits)  # never edits below-content id
    # Adopt the delta message per spec §A1(2) (mid 1 = the reposted "world").
    assert relay.cursor.message_ids == [1]
    assert relay.cursor.message_text_lens == [len("world")]
    assert relay.cursor.last_posted_len == len("world")
    assert relay._posted_len == len("world")
    assert relay._per_message_text == "world"


async def test_cold_reconcile_no_repost_when_fully_posted(tmp_path):
    """§A1(2) core F-DUP win: when ``last_posted_len`` shows the FULL narration
    already reached the wire, the cold reconcile reposts NOTHING (empty pending)
    — the every-turn duplicate the old full-repost produced is gone. The below-
    content message id is never edited."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("all posted narration"), _reply_tool_frame("R"),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[7],
        last_posted_len=len("all posted narration"),  # whole tail on the wire
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert rec.sends == []                              # nothing reposted
    assert all(mid != 7 for _t, mid, _x in rec.edits)  # never edits id 7


# ---------------------------------------------------------------------------
# spec §A1 RED test 6 — a checkpoint persists the WIRE high-water, not the
# in-memory length (which includes a throttled, never-posted suffix).
# ---------------------------------------------------------------------------


async def test_tool_frame_checkpoint_persists_wire_truth(tmp_path):
    """§A1 "Persist the TRUE wire high-water" (Sol r2-1a): a text frame posts a
    prefix, the next text frame is THROTTLE-HELD (advances ``_per_message_text``
    WITHOUT reaching the wire), then a tool-only assistant frame checkpoints. The
    persisted ``last_posted_len`` must equal the WIRE length (excludes the held
    suffix), and cold recovery must repost the held suffix exactly once.

    Frozen clock + a wide throttle window: "AAA"→send, "AAABBB"→edit (posts,
    arms the window at t=0), "AAABBBCCC"→edit THROTTLED (held, unposted); then a
    tool-only ``Read`` frame checkpoints the wire truth (6), NOT the in-memory 9.
    """
    rec, events = Recorder(), []
    _write_current(tmp_path, [
        _init(), _text("AAA"), _text("BBB"), _text("CCC"), _tool("Read"),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cur_path, rec, events, edit_throttle=10.0, _now=lambda: 0.0,
    )
    await relay.run()

    # "AAABBB" reached the wire (send + edit); "CCC" is throttle-held, unposted.
    assert relay._posted_len == 6
    assert relay._per_message_text == "AAABBBCCC"
    # The tool-only frame checkpointed the WIRE truth, not the in-memory length.
    saved = StreamCursor.load(cur_path)
    assert saved.last_posted_len == 6  # NOT 9 — excludes the held "CCC"

    # Cold recovery (FRESH relay + sequencer) reposts ONLY the held suffix once.
    rec2, events2 = Recorder(), []
    await _make_relay(tmp_path, cur_path, rec2, events2).run()
    assert [t for _tp, t in rec2.sends] == ["CCC"]
    assert sum(t == "CCC" for _tp, t in rec2.sends) == 1
