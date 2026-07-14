"""Tests for ``drivers.topic_stream`` — the NDJSON engagement-log → Telegram
topic relay (design §W1; Sol adversarial-review invariants B6/B7/B4/B5).

Each test drives a REAL temp file as the log and injects fake async
send/edit/delete recorders + an ``on_turn_event`` recorder, so the relay's
crash-safe segment cursor and at-least-once posting are exercised end-to-end.
Time is injected (``edit_throttle=0.0`` + a no-op ``_sleep``) — we never patch
``asyncio.sleep`` (the global-patch OOM lesson).
"""
from __future__ import annotations

import json
import logging
import os

from drivers.topic_stream import (
    SEGMENT_GAP,
    StreamCursor,
    TopicStreamRelay,
    extract_text_blocks,
    is_mutating_tooluse,
    iter_log_segments,
    parse_frame,
)


# ---------------------------------------------------------------------------
# Fakes + frame builders.
# ---------------------------------------------------------------------------


class Recorder:
    """Fake async Telegram senders that record every call."""

    def __init__(self) -> None:
        self.sends: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.deletes: list[tuple[int, int]] = []
        self._next_id = 1
        self.send_fails = 0  # first N send() calls raise
        self.edit_fails = 0  # first N edit() calls return False

    async def send(self, topic_id: int, text: str) -> int | None:
        if self.send_fails > 0:
            self.send_fails -= 1
            raise RuntimeError("telegram send boom")
        self.sends.append((topic_id, text))
        mid = self._next_id
        self._next_id += 1
        return mid

    async def edit(self, topic_id: int, message_id: int, text: str) -> bool:
        if self.edit_fails > 0:
            self.edit_fails -= 1
            return False
        self.edits.append((topic_id, message_id, text))
        return True

    async def delete(self, topic_id: int, message_id: int) -> bool:
        self.deletes.append((topic_id, message_id))
        return True


async def _no_sleep(_seconds: float) -> None:
    return None


def _spawn(epoch: int) -> dict:
    return {"casa_control": "spawn", "epoch": epoch}


def _init(session_id: str = "sid-1") -> dict:
    return {"type": "system", "subtype": "init", "session_id": session_id}


def _text(*texts: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": t} for t in texts]},
    }


def _tool(name: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]},
    }


def _thinking(t: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": t}]},
    }


def _ratelimit() -> dict:
    return {"type": "rate_limit_event"}


def _result(subtype: str = "success") -> dict:
    return {"type": "result", "subtype": subtype}


def _write_current(log_dir, frames) -> list[int]:
    """Write *frames* as NDJSON to ``<log_dir>/current``; return end offsets.

    ``frames`` items may be dicts (JSON-encoded), ``str`` (raw line, newline
    appended), or ``bytes`` (written verbatim). Returns the byte offset just
    after each written frame.
    """
    path = os.path.join(str(log_dir), "current")
    offsets: list[int] = []
    with open(path, "wb") as fh:
        for fr in frames:
            if isinstance(fr, bytes):
                fh.write(fr)
            elif isinstance(fr, str):
                fh.write(fr.encode("utf-8") + b"\n")
            else:
                fh.write(json.dumps(fr).encode("utf-8") + b"\n")
            offsets.append(fh.tell())
    return offsets


def _ident(path) -> list[int]:
    st = os.stat(path)
    return [st.st_dev, st.st_ino]


def _make_relay(log_dir, cursor_path, rec, events, reply_texts=None, **kw):
    return TopicStreamRelay(
        engagement_id="eng-1",
        topic_id=42,
        log_dir=str(log_dir),
        cursor_path=str(cursor_path),
        send_message=rec.send,
        edit_message=rec.edit,
        delete_message=rec.delete,
        on_turn_event=lambda kind, payload: events.append((kind, payload)),
        reply_texts=reply_texts or (lambda: set()),
        edit_throttle=0.0,
        _sleep=_no_sleep,
        **kw,
    )


# ---------------------------------------------------------------------------
# Pure-function classifiers.
# ---------------------------------------------------------------------------


def test_parse_frame_json_and_garbage():
    assert parse_frame(b'{"a": 1}') == {"a": 1}
    assert parse_frame(b"not json at all") is None
    assert parse_frame(b"[1, 2, 3]") is None  # non-object


def test_extract_text_blocks_text_only():
    frame = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "name": "Edit", "input": {}},
                {"type": "thinking", "thinking": "secret reasoning"},
                {"type": "text", "text": "world"},
            ]
        },
    }
    assert extract_text_blocks(frame) == ["hello", "world"]
    assert extract_text_blocks(_tool("Read")) == []
    assert extract_text_blocks({"type": "result"}) == []


def test_is_mutating_tooluse_allowlist():
    assert is_mutating_tooluse(_tool("Edit")) == (True, "Edit")
    assert is_mutating_tooluse(_tool("Bash"))[0] is True
    assert is_mutating_tooluse(_tool("Read")) == (False, "")
    assert is_mutating_tooluse(_tool("mcp__casa-engagement-channel__reply")) == (
        False,
        "",
    )
    # Unknown mcp__* tool → mutating.
    assert is_mutating_tooluse(_tool("mcp__something__do_it")) == (
        True,
        "mcp__something__do_it",
    )
    assert is_mutating_tooluse(_text("hi")) == (False, "")


# ---------------------------------------------------------------------------
# Live streaming.
# ---------------------------------------------------------------------------


async def test_streams_assistant_text_single_edited_message(tmp_path):
    rec, events = Recorder(), []
    offs = _write_current(
        tmp_path, [_spawn(1), _init(), _text("Hello "), _text("world"), _result()]
    )
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    assert len(rec.sends) == 1  # exactly one message opened
    assert rec.sends[0][1] == "Hello "
    assert len(rec.edits) >= 1
    assert rec.edits[-1][2] == "Hello world"  # final == joined
    cur = StreamCursor.load(cursor)
    assert cur.current["offset"] == offs[-1]  # advanced to file size
    assert ("spawn", {"epoch": 1}) in events


async def test_ignores_tools_thinking_unknown_and_ratelimit(tmp_path):
    rec, events = Recorder(), []
    _write_current(
        tmp_path,
        [
            _init(),
            _tool("Read"),
            _thinking("private chain of thought"),
            _ratelimit(),
            "this is not json",
            _result(),
        ],
    )
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    assert rec.sends == []
    assert rec.edits == []


async def test_mutating_tooluse_emits_event_not_stream(tmp_path):
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _tool("Edit"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    assert ("mutating_tool", {"tool": "Edit"}) in events
    assert rec.sends == []  # never streamed


async def test_rollover_past_3900_two_messages(tmp_path):
    rec, events = Recorder(), []
    a = "a" * 2500
    b = "b" * 2500  # 5000 total > 3900 → rolls to a second message
    _write_current(tmp_path, [_init(), _text(a), _text(b), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    assert len(rec.sends) == 2  # SEND recorder shows two messages
    for _topic, text in rec.sends:
        assert len(text) <= 3900


async def test_single_huge_block_splits_into_three_messages(tmp_path):
    rec, events = Recorder(), []
    huge = "x" * 10000  # one block → ceil(10000/3900) = 3 messages
    _write_current(tmp_path, [_init(), _text(huge), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    assert len(rec.sends) >= 3
    for _topic, text in rec.sends:
        assert len(text) <= 3900
    assert sum(len(t) for _tp, t in rec.sends) == 10000


async def test_open_midturn_checkpoint_tracks_message_ids(tmp_path):
    rec, events = Recorder(), []
    # Rollover to two messages, but NO result frame → turn stays open.
    _write_current(tmp_path, [_init(), _text("y" * 5000)])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)
    await relay.run()

    assert relay.cursor.message_ids == [1, 2]
    assert StreamCursor.load(cursor).message_ids == [1, 2]


async def test_finalize_edits_only_final_chunk_after_rollover(tmp_path):
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("m" * 5000), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    # The final edit targets the LAST message with its OWN chunk, never the
    # whole 5000-char turn text.
    assert rec.edits, "expected a finalize edit"
    last_topic, last_id, last_text = rec.edits[-1]
    assert last_id == 2  # the second (last) message, not the first
    assert len(last_text) <= 3900
    assert last_text != "m" * 5000


async def test_finalize_persists_closed_turn_checkpoint(tmp_path):
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("done"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    cur = StreamCursor.load(cursor)
    assert cur.message_ids == []
    assert cur.turn_start == cur.current
    assert cur.current["offset"] == offs[-1]


async def test_finalize_edit_retries_before_closing_checkpoint(tmp_path):
    """B2 (Sol r1): the finalize edit must honor the at-least-once contract —
    a transient Telegram failure retries via the bounded backoff and the
    fragment IS delivered before the closed-turn checkpoint advances past
    ``result``. The streaming path for a single text block is a SEND, so
    ``edit_fails`` bites only the finalize edit here."""
    rec, events = Recorder(), []
    rec.edit_fails = 2  # finalize's closing edit fails twice, then succeeds
    offs = _write_current(tmp_path, [_init(), _text("hello"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    # The final fragment landed (retried, not silently dropped).
    assert rec.edits and rec.edits[-1][2] == "hello"
    cur = StreamCursor.load(cursor)
    # Checkpoint closed ONLY after the edit succeeded — normal (non-drop) close.
    assert cur.message_ids == []
    assert cur.dropped_through is None
    assert cur.current["offset"] == offs[-1]


async def test_finalize_persistent_edit_failure_drops_once(tmp_path, caplog):
    """B2 (Sol r1): if the finalize edit fails persistently, the turn enters
    the documented drop path — warn ONCE, checkpoint closes via ``dropped_through``
    (no silent loss), and there is no infinite retry loop."""
    rec, events = Recorder(), []
    rec.edit_fails = 10_000  # finalize edit never succeeds
    offs = _write_current(tmp_path, [_init(), _text("hello"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    with caplog.at_level(logging.WARNING):
        await _make_relay(tmp_path, cursor, rec, events).run()

    cur = StreamCursor.load(cursor)
    # Dropped through the terminal coordinate (not silently advanced).
    assert cur.dropped_through is not None
    assert cur.dropped_through["offset"] == offs[-1]
    assert cur.current["offset"] == offs[-1]
    assert cur.message_ids == []
    drop_warnings = [r for r in caplog.records if "dropping remainder" in r.message]
    assert len(drop_warnings) == 1  # exactly one WARNING


async def test_restart_after_successful_dedup_no_ghost_edit(tmp_path):
    # Turn 1: single message whose whole text == a reply → de-dup deletes it.
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("ping"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    reply = lambda: {"ping"}
    await _make_relay(tmp_path, cursor, rec, events, reply_texts=reply).run()
    assert rec.deletes == [(42, 1)]  # sole message deleted
    assert StreamCursor.load(cursor).message_ids == []

    # Turn 2 appended; a NEW relay resumes from the closed checkpoint.
    with open(os.path.join(str(tmp_path), "current"), "ab") as fh:
        for fr in (_init("sid-2"), _text("second turn"), _result()):
            fh.write(json.dumps(fr).encode("utf-8") + b"\n")
    rec2, events2 = Recorder(), []
    rec2._next_id = 100  # turn 2's new message gets a fresh, distinct id
    await _make_relay(tmp_path, cursor, rec2, events2).run()

    # ZERO edits against the deleted id (1); the second turn streams cleanly
    # into its OWN new message (id 100).
    assert all(mid != 1 for _t, mid, _x in rec2.edits)
    assert rec2.sends and rec2.sends[0][1] == "second turn"
    assert StreamCursor.load(cursor).message_ids == []
    _ = offs


# ---------------------------------------------------------------------------
# Failure / drop.
# ---------------------------------------------------------------------------


async def test_cursor_advances_only_after_success(tmp_path):
    rec, events = Recorder(), []
    rec.edit_fails = 2  # first two edits fail, third succeeds
    _write_current(tmp_path, [_init(), _text("first"), _text("second")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)
    await relay.run()

    # The send for msg 1 + the retried edit for the second text both landed;
    # the cursor advanced past the second text only after the edit succeeded.
    assert rec.sends[0][1] == "first"
    assert rec.edits[-1][2] == "firstsecond"
    saved = StreamCursor.load(cursor)
    assert saved.message_ids == [1]
    assert saved.last_posted_len == len("firstsecond")


async def test_persistent_failure_drops_through_terminal(tmp_path, caplog):
    rec, events = Recorder(), []
    rec.send_fails = 10_000  # every send fails forever
    offs = _write_current(tmp_path, [_init(), _text("a"), _text("b"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    with caplog.at_level(logging.WARNING):
        await _make_relay(tmp_path, cursor, rec, events).run()

    cur = StreamCursor.load(cursor)
    assert cur.dropped_through is not None
    assert cur.dropped_through["offset"] == offs[-1]  # terminal coordinate
    assert cur.current["offset"] == offs[-1]
    assert rec.sends == []  # nothing ever posted
    drop_warnings = [r for r in caplog.records if "dropping remainder" in r.message]
    assert len(drop_warnings) == 1  # exactly one WARNING


# ---------------------------------------------------------------------------
# Recovery.
# ---------------------------------------------------------------------------


async def test_recovery_replays_turn_edits_last_id(tmp_path):
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("hello world"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # Mid-turn: message 7 posted, cursor checkpointed through the text frame.
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},
        message_ids=[7],
        last_posted_len=len("hello world"),
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert rec.sends == []  # no new message opened
    assert rec.edits, "expected a reconcile / final edit"
    assert all(mid == 7 for _t, mid, _x in rec.edits)  # only the last id


async def test_recovery_dropped_through_is_honored(tmp_path):
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("a"), _text("b"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # A previous run dropped the whole turn (dropped_through == terminal).
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[],
        dropped_through={"segment": seg, "offset": offs[-1]},
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert rec.sends == []  # no re-post of dropped bytes
    assert rec.edits == []


async def test_crash_after_post_before_persist_dup_send(tmp_path):
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("fragment")])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # Simulate crash-after-post-before-persist: init was checkpointed but the
    # text frame's post landed WITHOUT the cursor being saved (message_ids empty,
    # current still at the init boundary).
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[0]},
        message_ids=[],
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    # The fragment is RE-SENT — documented at-least-once duplicate.
    assert [t for _tp, t in rec.sends] == ["fragment"]


async def test_recovery_replays_turn_edits_last_id_after_more_live_text(tmp_path):
    # Sanity companion: replay prefix + a live text frame past current.
    rec, events = Recorder(), []
    offs = _write_current(
        tmp_path, [_init(), _text("hello"), _text(" more"), _result()]
    )
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},  # through "hello"
        message_ids=[7],
        last_posted_len=len("hello"),
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert rec.sends == []
    assert rec.edits[-1] == (42, 7, "hello more")


# ---------------------------------------------------------------------------
# Segments: rotation / archive-span / gap.
# ---------------------------------------------------------------------------


async def test_rotation_drains_old_segment(tmp_path):
    log_dir = str(tmp_path)
    current = os.path.join(log_dir, "current")
    # First half in `current` (inode X).
    with open(current, "wb") as fh:
        fh.write(b'{"n": 1}\n{"n": 2}\n')

    gen = iter_log_segments(log_dir, {"segment": [0, 0], "offset": 0})
    first = await gen.__anext__()  # opens current(X), holds the fd

    # Rotate: current(X) → @a.s, new current(Y) with the second half.
    os.rename(current, os.path.join(log_dir, "@a.s"))
    with open(current, "wb") as fh:
        fh.write(b'{"n": 3}\n{"n": 4}\n')

    rest = [item async for item in gen]
    lines = [first[2]] + [r[2] for r in rest]
    ns = [json.loads(x)["n"] for x in lines]
    assert ns == [1, 2, 3, 4]  # BOTH halves seen via held-fd drainage


async def test_restart_turn_spans_archive_then_current(tmp_path):
    log_dir = str(tmp_path)
    # Archive @a.s holds the turn start (init + first text); current holds the rest.
    archive = os.path.join(log_dir, "@a.s")
    with open(archive, "wb") as fh:
        for fr in (_init(), _text("part-one ")):
            fh.write(json.dumps(fr).encode("utf-8") + b"\n")
    seg_arch = _ident(archive)
    current = os.path.join(log_dir, "current")
    with open(current, "wb") as fh:
        for fr in (_text("part-two"), _result()):
            fh.write(json.dumps(fr).encode("utf-8") + b"\n")

    rec, events = Recorder(), []
    cur_path = tmp_path / ".stream_cursor.json"
    StreamCursor(
        turn_start={"segment": seg_arch, "offset": 0},
        current={"segment": seg_arch, "offset": 0},  # nothing applied yet
        message_ids=[],
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    # Full turn recovered across the boundary; a single message carries it.
    assert len(rec.sends) == 1
    assert rec.edits[-1][2] == "part-one part-two"


async def test_rollover_plus_restart_recovers_editing_final_id(tmp_path):
    rec, events = Recorder(), []
    # A >3900 turn spanning two messages (ids 7 and 9), persisted mid-turn.
    big = "z" * 5000
    offs = _write_current(tmp_path, [_init(), _text(big), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},  # through the text frame
        message_ids=[7, 9],
        last_posted_len=len(big) - 3900,
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert rec.sends == []  # never re-posts 7
    assert rec.edits, "expected edits to the final id"
    assert all(mid == 9 for _t, mid, _x in rec.edits)  # EDITS 9, the final id


async def test_invisible_frames_checkpoint_and_no_spawn_replay(tmp_path):
    rec, events = Recorder(), []
    # spawn + init + a tool-only frame: no visible text, all invisible.
    offs = _write_current(tmp_path, [_spawn(1), _init(), _tool("Read")])
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    assert [k for k, _p in events].count("spawn") == 1  # emitted once, live
    assert relay.cursor.current["offset"] == offs[-1]  # all advanced the cursor

    # Restart from the saved cursor → no spawn re-emitted (it precedes turn_start).
    rec2, events2 = Recorder(), []
    await _make_relay(tmp_path, cur_path, rec2, events2).run()
    assert [k for k, _p in events2].count("spawn") == 0


async def test_recovery_suppresses_inturn_checkpointed_spawn_side_effect(tmp_path):
    rec, events = Recorder(), []
    # Turn: init, text (posted), an in-turn RESPAWN control frame, a mutating
    # tool, then more text — ALL checkpointed (<= current).
    frames = [
        _init(),
        _text("alpha"),
        _spawn(2),  # abnormal in-turn respawn
        _tool("Edit"),  # in-turn mutating tool
        _text(" beta"),
    ]
    offs = _write_current(tmp_path, frames)
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},  # everything <= current
        message_ids=[5],
        last_posted_len=len("alpha"),
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    # Text state rebuilt → reconcile edits the last id with the full text.
    assert rec.sends == []
    assert rec.edits[-1] == (42, 5, "alpha beta")
    # NO side effects re-fired for the in-turn checkpointed frames.
    assert [k for k, _p in events].count("spawn") == 0
    assert [k for k, _p in events].count("mutating_tool") == 0


async def test_segment_gap_warns_and_resumes(tmp_path, caplog):
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("recovered"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    # turn_start points at a segment that is NOT on disk (rotated out).
    StreamCursor(
        turn_start={"segment": [999, 999], "offset": 20},
        current={"segment": [999, 999], "offset": 40},
        message_ids=[],
    ).save(cur_path)

    with caplog.at_level(logging.WARNING):
        await _make_relay(tmp_path, cur_path, rec, events).run()

    assert any("retention gap" in r.message for r in caplog.records)
    # Resumed at current offset 0 → the whole live turn was processed.
    assert rec.sends and rec.sends[0][1] == "recovered"
    assert StreamCursor.load(cur_path).current["offset"] == offs[-1]


async def test_reply_dedup_deletes_identical_final(tmp_path):
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("the answer is 42"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    reply = lambda: {"the answer is 42"}
    await _make_relay(tmp_path, cur_path, rec, events, reply_texts=reply).run()

    assert rec.deletes == [(42, 1)]  # identical final text deleted


async def test_spawn_frame_emits_event(tmp_path):
    rec, events = Recorder(), []
    _write_current(tmp_path, [_spawn(3)])
    cur_path = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert ("spawn", {"epoch": 3}) in events


# ---------------------------------------------------------------------------
# Cursor persistence round-trip.
# ---------------------------------------------------------------------------


def test_stream_cursor_load_absent_is_zero(tmp_path):
    cur = StreamCursor.load(tmp_path / "nope.json")
    assert cur.turn_start == {"segment": [0, 0], "offset": 0}
    assert cur.current == {"segment": [0, 0], "offset": 0}
    assert cur.message_ids == []
    assert cur.dropped_through is None


def test_stream_cursor_save_load_roundtrip(tmp_path):
    path = tmp_path / "c.json"
    StreamCursor(
        turn_start={"segment": [1, 2], "offset": 3},
        current={"segment": [1, 2], "offset": 9},
        message_ids=[7, 9],
        last_posted_len=11,
        dropped_through={"segment": [1, 2], "offset": 9},
    ).save(path)
    back = StreamCursor.load(path)
    assert back.message_ids == [7, 9]
    assert back.current == {"segment": [1, 2], "offset": 9}
    assert back.dropped_through == {"segment": [1, 2], "offset": 9}
