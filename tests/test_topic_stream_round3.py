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

import json
import os

import pytest

from channels.output_sequencer import REPLY_TOOL, projection_hash
from drivers.topic_stream import StreamCursor
from test_topic_stream import (
    Recorder,
    _ident,
    _init,
    _make_relay,
    _reply_tool_frame,
    _result,
    _text,
    _tool,
    _write_current,
)

pytestmark = pytest.mark.asyncio


def _append_current(log_dir, frames) -> None:
    """Append NDJSON *frames* to ``<log_dir>/current`` (models a live poll)."""
    path = os.path.join(str(log_dir), "current")
    with open(path, "ab") as fh:
        for fr in frames:
            fh.write(json.dumps(fr).encode("utf-8") + b"\n")


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


# ---------------------------------------------------------------------------
# spec §A1(1) — WARM re-entry (the every-turn duplicate fix). The relay object
# survives the driver's 0.5s poll; a clean re-entry resumes IN PLACE from
# ``_read_coord`` (no cursor reload, no state reset, live, no replay/reconcile),
# so ``_reconcile`` is confined to genuine process/task restarts.
# ---------------------------------------------------------------------------


def _seal_via_discrete(relay, rec, text: str = "R", request_id: str = "d1") -> None:
    """Register + arm a reply intent on the SHARED sequencer and post it — the
    discrete post seals the open narration (as an ask/reply ingress would)."""
    h = projection_hash(REPLY_TOOL, {"text": text})

    async def poster():
        return await rec.send(42, f"[ask]{text}")

    relay.sequencer.register_intent(
        request_id=request_id, tool_name=REPLY_TOOL, projection_hash=h, poster=poster,
    )
    relay.sequencer.arm_intent(request_id)
    return h


async def test_warm_reentry_seal_then_new_frame_posts_only_delta(tmp_path):
    """§A1 RED 1: narration streams → ``run()`` returns at EOF (warm latched) →
    an ask intent posts through the SHARED sequencer (seals narration) → warm
    ``run()`` with ONE new text frame → the new message carries ONLY the delta;
    no wire message repeats the pre-seal narration."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("hello narration")])  # open turn
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()
    assert relay._warm is True
    assert [t for _tp, t in rec.sends] == ["hello narration"]

    h = _seal_via_discrete(relay, rec)
    assert await relay.sequencer.post_for_block(REPLY_TOOL, h) == "posted"

    _append_current(tmp_path, [_text(" more")])
    await relay.run()  # WARM re-entry

    assert [t for _tp, t in rec.sends] == ["hello narration", "[ask]R", " more"]
    assert sum("hello narration" in t for _tp, t in rec.sends) == 1
    assert all("hello narration" not in text for _tp, _mid, text in rec.edits)


async def test_warm_reentry_pure_poll_posts_nothing(tmp_path):
    """§A1 RED 2: a pure poll tick (warm re-entry with NO new frames) performs
    zero sends and zero edits."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("streaming body")])
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()
    assert relay._warm is True
    before = (list(rec.sends), list(rec.edits))

    await relay.run()  # pure poll — nothing new on disk

    assert (rec.sends, rec.edits) == before


async def test_warm_reentry_inbound_seal_no_repost(tmp_path):
    """§A1 RED 3: ``advance_high_water_for_inbound`` seals between polls → the
    next warm ``run()`` never reposts the sealed narration; a new text frame
    opens a fresh message with only its delta."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("narr body")])
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()
    assert [t for _tp, t in rec.sends] == ["narr body"]

    await relay.sequencer.advance_high_water_for_inbound(999)  # inbound seal

    _append_current(tmp_path, [_text(" tail")])
    await relay.run()

    assert [t for _tp, t in rec.sends] == ["narr body", " tail"]
    assert sum("narr body" in t for _tp, t in rec.sends) == 1
    assert all("narr body" not in text for _tp, _mid, text in rec.edits)


async def test_warm_invalidated_by_exception_next_run_cold(tmp_path):
    """§A1 RED 5: an exception escaping the warm frame loop clears ``_warm``, so
    the next ``run()`` takes the COLD path — observable via a reconcile that only
    the recovering cold path performs."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("body")])  # open turn persisted
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()
    assert relay._warm is True

    _append_current(tmp_path, [_text(" more")])

    async def _raising(*_a, **_k):
        raise RuntimeError("frame boom")

    relay._handle_frame = _raising
    with pytest.raises(RuntimeError):
        await relay.run()
    assert relay._warm is False  # warm latch invalidated

    del relay._handle_frame  # restore the bound method
    called = {"n": 0}
    orig = relay._reconcile

    async def _spy():
        called["n"] += 1
        await orig()

    relay._reconcile = _spy
    await relay.run()  # COLD (recovering) — reconcile fires
    assert called["n"] >= 1


async def test_replay_only_cold_run_seeds_read_coord_warm_resumes(tmp_path):
    """§A1 RED 7 (Sol r2-1b): a cold run that reaches EOF entirely in REPLAY must
    still seed ``_read_coord`` (from the last replayed frame) before latching
    warm; the next warm run resumes there without re-applying replayed frames."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("recovered"), _reply_tool_frame("R"),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},  # all replayed, open turn
        message_ids=[7], last_posted_len=0,
    ).save(cur_path)
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()  # cold: entirely replay → reconcile at EOF

    assert rec.sends == [(42, "recovered")]  # conservative seal reposts once
    assert relay._warm is True
    assert relay._read_coord == {"segment": seg, "offset": offs[-1]}

    before = (list(rec.sends), list(rec.edits))
    await relay.run()  # warm poll — resumes at _read_coord, no re-apply
    assert (rec.sends, rec.edits) == before


async def test_zero_frame_closed_turn_latches_warm_from_current(tmp_path):
    """§A1 RED 6a (Sol r3-3): a zero-frame poll at EXACT EOF over a CLOSED-turn
    boundary (``message_ids == []``) latches warm, seeding ``_read_coord`` from
    ``cursor.current``."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("done"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": offs[-1]},  # at EOF → zero frames
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[],
    ).save(cur_path)
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    assert rec.sends == [] and rec.edits == []
    assert relay._warm is True
    assert relay._read_coord == {"segment": seg, "offset": offs[-1]}


async def test_zero_frame_open_turn_does_not_latch_warm(tmp_path):
    """§A1 RED 6b (Sol r3-3): a zero-frame run over an OPEN turn does NOT latch
    warm — the next poll runs cold again."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("open")])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": offs[-1]},  # at EOF → zero frames
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[7],  # OPEN turn
    ).save(cur_path)
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    assert relay._warm is False
    assert relay._read_coord is None


async def test_gap_only_run_stays_cold(tmp_path):
    """§A1 RED 7 (Sol r3-3): a gap-only run (retention-gap sentinel, then zero
    real frames) does NOT latch warm — the sentinel never becomes a coordinate."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [])  # empty current
    cur_path = tmp_path / ".stream_cursor.json"
    StreamCursor(
        turn_start={"segment": [1234, 999999999], "offset": 0},  # phantom → gap
        current={"segment": [1234, 999999999], "offset": 0},
        message_ids=[],
    ).save(cur_path)
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    assert relay._warm is False
    assert relay._read_coord is None


async def test_stale_dropped_through_cleared_frames_flow(tmp_path):
    """§A1 RED 7 (Sol r3-3): a persisted ``dropped_through`` whose segment has
    rotated off disk ranks at infinity in ``_seg_rank`` → every future frame
    compares ``<= dropped_through`` and is skipped forever. Cold start must CLEAR
    the stale entry so frames flow again."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("flowing"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": 0},
        message_ids=[],
        dropped_through={"segment": [1234, 999999999], "offset": 0},  # phantom
    ).save(cur_path)
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    assert relay.cursor.dropped_through is None
    assert [t for _tp, t in rec.sends] == ["flowing"]


async def test_warm_throttled_hold_flushes_no_reread_duplication(tmp_path):
    """§A1 Sol r1-1: warm re-entry resumes from ``_read_coord`` (past the
    throttle-held frame), NOT from the behind-sitting ``cursor.current``. The
    held suffix stays in memory and flushes on the next edit — never re-read and
    duplicated (the very bug class A1 fixes)."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("AAA"), _text("BBB"), _text("CCC")])
    cur_path = tmp_path / ".stream_cursor.json"
    clock = {"t": 0.0}
    relay = _make_relay(
        tmp_path, cur_path, rec, events, edit_throttle=1.0, _now=lambda: clock["t"],
    )
    await relay.run()

    # AAA sent, BBB edited (arms the window at t=0), CCC edit THROTTLE-HELD.
    assert relay._posted_len == 6
    assert relay._per_message_text == "AAABBBCCC"
    assert [t for _tp, t in rec.sends] == ["AAA"]
    assert relay._warm is True

    clock["t"] = 10.0  # past the throttle window
    _append_current(tmp_path, [_text("DDD")])
    await relay.run()  # WARM — the held "CCC" flushes with "DDD"

    assert relay._per_message_text == "AAABBBCCCDDD"  # not "AAABBBCCCCCCDDD"
    assert rec.edits[-1] == (42, 1, "AAABBBCCCDDD")
    assert [t for _tp, t in rec.sends] == ["AAA"]  # no re-send / duplication
