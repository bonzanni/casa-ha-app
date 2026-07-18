"""R3 — narration spacing at the ``message.id`` boundary (v0.89.0).

Successive narration segments from DIFFERENT assistant messages were
concatenated with NO separator, producing run-ons like ``questions.Good`` and
``ext.Appl``. R3 inserts a ``\\n\\n`` separator at the boundary between distinct
assistant ``message.id``\\ s — IDENTICALLY across the three narration consumers:
the live path, the ``_replay_text`` cold-recovery path, and the held-anchor-
buffer flush.

The boundary signal is the structural ``message.id`` change (NO content
inspection / prose sniffing). The contract — VERIFIED against a captured
real-CLI fixture (``fixtures/engagement_cli_frames.ndjson``) — is that every
assistant frame carries ``message.id``; there is never more than one non-empty
text block per assistant message; and a ``message.id`` never carries non-empty
text in more than one frame. A DISTINCT ``message.id`` == a new segment.

These tests drive a REAL ``TopicStreamRelay`` over a REAL temp NDJSON log (the
``test_topic_stream`` harness) and REAL captured CLI frames; clocks are injected
and ``asyncio.sleep`` is never patched (the CLAUDE.md memory-cage rule).
"""
from __future__ import annotations

import json
import os

from channels.output_sequencer import ASK_TOOL, projection_hash
from drivers.topic_stream import extract_narration
from test_topic_stream import (
    Recorder,
    _init,
    _make_relay,
    _result,
    _spawn,
    _thinking,
    _tool_in,
    _write_current,
)

# ``asyncio_mode = auto`` (pytest.ini) marks the async tests; sync tests
# (the extractor + tripwire) stay unmarked.

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "engagement_cli_frames.ndjson"
)


# ---------------------------------------------------------------------------
# Frame builders with an explicit message.id (the R3 boundary signal).
# ---------------------------------------------------------------------------


def _text_id(message_id: str, *texts: str) -> dict:
    """An assistant text frame carrying an explicit ``message.id``."""
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "content": [{"type": "text", "text": t} for t in texts],
        },
    }


def _fast_sequencer(rec):
    """A REAL ``OutputSequencer`` with an injected clock (mirrors
    ``test_anchor_narration_buffer._fast_sequencer``)."""
    from channels.output_sequencer import OutputSequencer

    clock = {"t": 0.0}

    async def _tick_sleep(dt: float) -> None:
        clock["t"] += dt

    return OutputSequencer(
        engagement_id="eng-1", topic_id=42,
        send_message=rec.send, edit_message=rec.edit,
        _now=lambda: clock["t"], _sleep=_tick_sleep,
        slot_hold_s=0.05, intent_timeout_s=5.0, hold_poll_s=0.05,
    )


def _anchor_ask(question: str = "Q?") -> dict:
    return _tool_in(ASK_TOOL, {"question": question})


def _ah(question: str = "Q?") -> str:
    return projection_hash(ASK_TOOL, {"question": question})


def _load_text_frames() -> list[tuple[str, str]]:
    """Every text-bearing assistant frame in the fixture as ``(message_id,
    text)`` in log order — read through the SAME extractor the relay uses."""
    out: list[tuple[str, str]] = []
    with open(FIXTURE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            narr = extract_narration(json.loads(line))
            if narr is not None and narr.text:
                out.append((narr.message_id, narr.text))
    return out


# ---------------------------------------------------------------------------
# The typed extractor.
# ---------------------------------------------------------------------------


def test_extract_narration_reads_message_id_and_text():
    narr = extract_narration(_text_id("msg_A", "hello world"))
    assert narr is not None
    assert narr.message_id == "msg_A"
    assert narr.text == "hello world"
    # Non-assistant frames → None.
    assert extract_narration({"type": "result"}) is None
    assert extract_narration(_spawn(1)) is None
    # Thinking-only assistant frame → present, but empty text.
    tnarr = extract_narration(_thinking("private cot"))
    assert tnarr is not None and tnarr.text == ""
    # Missing message.id degrades to "" (an id that never changes → no sep).
    no_id = extract_narration(
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "x"}]}}
    )
    assert no_id.message_id == "" and no_id.text == "x"


# ---------------------------------------------------------------------------
# Live path — the run-on fix.
# ---------------------------------------------------------------------------


async def test_distinct_message_ids_separated_by_blank_line(tmp_path):
    """Two text frames with DIFFERENT message.ids get a ``\\n\\n`` separator —
    the ``questions.Good`` run-on becomes ``questions.\\n\\nGood``."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [
        _init(),
        _text_id("m1", "...into the design questions."),
        _text_id("m2", "Good — the project is a clean slate."),
        _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    final = rec.edits[-1][2] if rec.edits else rec.sends[-1][1]
    assert "questions.\n\nGood" in final
    assert "questions.Good" not in final
    # The FIRST segment carries no LEADING separator.
    assert not final.startswith("\n\n")


async def test_first_segment_has_no_leading_separator(tmp_path):
    """A single-message turn is byte-identical to today (no id change → no
    separator is ever inserted)."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [
        _init(), _text_id("only", "Just one segment."), _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()
    assert rec.sends[0][1] == "Just one segment."
    assert "\n\n" not in rec.sends[0][1]


# ---------------------------------------------------------------------------
# Cold-replay byte-identity (the load-bearing parity point).
# ---------------------------------------------------------------------------


async def test_cold_replay_of_multi_id_turn_is_byte_identical(tmp_path):
    """A crash mid-turn, then a brand-new relay cold-recovers over the SAME
    cursor+log: the reconstructed per-message text and the byte-cursor
    (``message_text_lens`` / ``_posted_len``) match the live relay EXACTLY,
    separators included. A desync here breaks the round-4 checkpoint cursor."""
    frames = [
        _spawn(1), _init(),
        _text_id("m1", "First segment."),
        _text_id("m2", "Second segment."),
        _text_id("m3", "Third segment."),
    ]
    cursor = tmp_path / ".stream_cursor.json"

    # LIVE: an OPEN turn (no result) posts the narration and persists the cursor.
    rec_live, ev_live = Recorder(), []
    _write_current(tmp_path, frames)
    live = _make_relay(tmp_path, cursor, rec_live, ev_live)
    await live.run()
    assert live._per_message_text == (
        "First segment.\n\nSecond segment.\n\nThird segment."
    )

    # REPLAY: a completely NEW relay on the SAME on-disk cursor+log → cold
    # recovery replays m1/m2/m3 through ``_replay_text`` and reconstructs.
    rec_re, ev_re = Recorder(), []
    replay = _make_relay(tmp_path, cursor, rec_re, ev_re)
    await replay.run()

    assert replay._per_message_text == live._per_message_text  # byte-identical
    assert replay.cursor.message_text_lens == live.cursor.message_text_lens
    assert replay.cursor.message_ids == live.cursor.message_ids
    assert replay._posted_len == live._posted_len


async def test_cold_replay_multi_message_rollover_reconstructs_with_seps(tmp_path):
    """Separators are ordinary narration bytes that flow through the _MSG_MAX
    rollover: a turn that rolls into two Telegram messages recovers byte-
    identically (the separator lands wherever the rollover places it)."""
    big = "x" * 2000
    frames = [
        _init(),
        _text_id("a", big),   # message 1 (2000 + sep bytes)
        _text_id("b", big),   # rolls past _MSG_MAX into message 2
    ]
    cursor = tmp_path / ".stream_cursor.json"
    rec_live, ev = Recorder(), []
    _write_current(tmp_path, frames)
    live = _make_relay(tmp_path, cursor, rec_live, ev)
    await live.run()
    live_lens = list(live.cursor.message_text_lens)
    live_ids = list(live.cursor.message_ids)

    rec_re, ev2 = Recorder(), []
    replay = _make_relay(tmp_path, cursor, rec_re, ev2)
    await replay.run()
    assert replay._per_message_text == live._per_message_text
    assert replay.cursor.message_text_lens == live_lens
    assert replay.cursor.message_ids == live_ids


# ---------------------------------------------------------------------------
# Held-anchor-buffer flush — parity with the live path across the boundary.
# ---------------------------------------------------------------------------


async def test_held_buffer_flush_separates_message_ids_like_live(tmp_path):
    """anchor → two DISTINCT-id prose frames buffered → tool_use FLUSHES: the
    flushed narration separates the two message.ids with ``\\n\\n``, exactly as
    an un-suppressed live run of the same two frames would."""
    rec, events = Recorder(), []
    seq = _fast_sequencer(rec)
    _write_current(tmp_path, [
        _init(), _anchor_ask("Q?"),
        _text_id("m1", "Alpha part."),
        _text_id("m2", "Beta part."),
        _tool_in("Bash", {"command": "ls"}),
        _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500, _ah("Q?")),
    )
    await relay.run()

    # The buffer flushed as ONE narration send carrying both segments + sep.
    narration = [t for _tp, t in rec.sends]
    assert narration == ["Alpha part.\n\nBeta part."]

    # PARITY: an un-suppressed live run of the SAME two frames yields the SAME
    # separated body (final growing message).
    rec2, ev2 = Recorder(), []
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    _write_current(live_dir, [
        _init(), _text_id("m1", "Alpha part."),
        _text_id("m2", "Beta part."), _result(),
    ])
    await _make_relay(
        live_dir, live_dir / ".stream_cursor.json", rec2, ev2,
    ).run()
    live_final = rec2.edits[-1][2] if rec2.edits else rec2.sends[-1][1]
    assert live_final == "Alpha part.\n\nBeta part."


# ---------------------------------------------------------------------------
# Real captured-CLI fixture: behaviour + the tripwire contract.
# ---------------------------------------------------------------------------


async def test_real_fixture_frames_are_separated_no_runons(tmp_path):
    """Drive the relay with the FIRST real consecutive text-bearing frames from
    the captured CLI fixture (each a distinct message.id) and assert the exact
    ``questions.Good`` run-on the fixture would otherwise produce is gone."""
    frames = _load_text_frames()[:4]
    assert len(frames) == 4, "fixture unexpectedly short"
    ids = [mid for mid, _ in frames]
    assert len(set(ids)) == 4, "expected 4 distinct message.ids"

    rec, events = Recorder(), []
    _write_current(
        tmp_path,
        [_init()] + [_text_id(mid, text) for mid, text in frames] + [_result()],
    )
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    final = rec.edits[-1][2] if rec.edits else rec.sends[-1][1]
    expected = "\n\n".join(text for _mid, text in frames)
    assert final == expected
    # The exact brief example, from real frames.
    assert "questions.\n\nGood" in final
    assert "questions.Good" not in final


def test_fixture_tripwire_contract():
    """FAIL LOUDLY if a future CLI breaks the message.id boundary contract the
    R3 separator rule depends on: every assistant frame carries a ``message.id``;
    at most ONE non-empty text block per frame; and NO ``message.id`` carries
    non-empty text in more than one frame."""
    assert os.path.exists(FIXTURE), f"missing captured-CLI fixture: {FIXTURE}"
    seen_text_ids: set[str] = set()
    n_assistant = 0
    with open(FIXTURE, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            frame = json.loads(line)
            if frame.get("type") != "assistant":
                continue
            n_assistant += 1
            message = frame.get("message") or {}
            mid = message.get("id")
            assert isinstance(mid, str) and mid, (
                f"frame {lineno}: assistant frame missing message.id — the R3 "
                f"boundary signal is gone; revisit the separator rule"
            )
            nonempty = [
                b for b in message.get("content") or []
                if isinstance(b, dict) and b.get("type") == "text"
                and isinstance(b.get("text"), str) and b["text"].strip()
            ]
            assert len(nonempty) <= 1, (
                f"frame {lineno} (id {mid}): >1 non-empty text block — CLI "
                f"contract broke; the message.id boundary rule needs revisiting"
            )
            if nonempty:
                assert mid not in seen_text_ids, (
                    f"frame {lineno}: message.id {mid} carries non-empty text in "
                    f"a SECOND frame — same-id text repeat; contract broke"
                )
                seen_text_ids.add(mid)
    assert n_assistant > 0, "fixture has no assistant frames"
    assert len(seen_text_ids) > 1, "fixture needs >1 distinct text-bearing id"
