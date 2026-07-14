"""Tests for ``drivers.summary_controller`` — the per-engagement live SUMMARY
controller (v0.79.0 §5, engagement-topic UX).

Every §5 sentence is binding. Time is INJECTED (``_now``/``_sleep``) so the
elapsed tick and the ≥10s throttle are deterministic and we never patch the
global ``asyncio.sleep`` (the OOM memory-cage rule).
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from channels.output_sequencer import APPLIED, FAILED
from drivers.summary_controller import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_WAITING_APPROVAL,
    STATUS_WAITING_REPLY,
    STATUS_WORKING,
    SummaryController,
    activity_for_tool,
    extract_plan,
    format_elapsed,
    render_summary,
    sanitize_plan_subject,
)

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


class FakeSequencer:
    """Records ``edit_summary`` calls; returns APPLIED (or FAILED on demand)."""

    def __init__(self) -> None:
        self.edits: list[tuple[int, str]] = []
        self.fail_next = 0

    async def edit_summary(self, msg_id: int, text: str) -> str:
        if self.fail_next > 0:
            self.fail_next -= 1
            return FAILED
        self.edits.append((msg_id, text))
        return APPLIED


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


async def _park(_dt: float) -> None:
    """A ``_sleep`` that parks forever (until the tick task is cancelled) — so
    the background tick loop never spins; we drive ``_tick_body`` directly."""
    await asyncio.Event().wait()


def _make(
    seq: FakeSequencer | None = None,
    *,
    clock: Clock | None = None,
    goal_line: str = "fix login bug",
    open_qs=(),
    pin=None,
    message_id: int | None = 500,
    throttle_s: float = 10.0,
    tick_s: float = 60.0,
) -> SummaryController:
    seq = seq or FakeSequencer()
    clock = clock or Clock()
    return SummaryController(
        engagement_id="eng123",
        sequencer=seq,
        goal_line=goal_line,
        open_question_numbers=lambda: list(open_qs),
        pin_message=pin,
        message_id=message_id,
        _now=clock.now,
        _sleep=_park,
        throttle_s=throttle_s,
        tick_s=tick_s,
    )


# ---------------------------------------------------------------------------
# Pure helpers — activity mapping, plan, elapsed, layout.
# ---------------------------------------------------------------------------


class TestActivityMapping:
    @pytest.mark.parametrize(
        "tool,expected",
        [
            ("Read", "reading files"),
            ("Glob", "reading files"),
            ("Grep", "reading files"),
            ("Write", "editing files"),
            ("Edit", "editing files"),
            ("NotebookEdit", "editing files"),
            ("Bash", "running commands"),
            ("Task", "planning"),
            ("TaskOutput", "planning"),
            ("TodoWrite", "planning"),
            ("WebFetch", "researching"),
            ("WebSearch", "researching"),
            ("mcp__ha-control__call_service", "using ha-control tools"),
            ("mcp__casa-framework__emit_completion", "using casa-framework tools"),
            ("SomethingElse", "working"),
            ("", "working"),
        ],
    )
    def test_coarse_mapping(self, tool, expected):
        assert activity_for_tool(tool) == expected


class TestPlanSanitization:
    def test_unsafe_text_rejected(self):
        # A newline (C0) is UNSAFE — would break the single-line layout.
        assert sanitize_plan_subject("do a\nthing") == ""
        # Bidi override is UNSAFE.
        assert sanitize_plan_subject("safe‮text") == ""

    def test_sixty_char_cap(self):
        subj = "x" * 200
        assert len(sanitize_plan_subject(subj)) == 60

    def test_plain_subject_passthrough(self):
        assert sanitize_plan_subject("refactor auth") == "refactor auth"

    def test_non_str_and_empty(self):
        assert sanitize_plan_subject(None) == ""
        assert sanitize_plan_subject("") == ""

    def test_todowrite_counts_and_current(self):
        plan = extract_plan(
            "TodoWrite",
            {
                "todos": [
                    {"content": "a", "status": "completed"},
                    {"content": "b", "status": "in_progress",
                     "activeForm": "doing b"},
                    {"content": "c", "status": "pending"},
                ]
            },
        )
        assert plan == {"done": 1, "total": 3, "subject": "doing b"}

    def test_todowrite_sanitizes_current(self):
        plan = extract_plan(
            "TodoWrite",
            {"todos": [{"content": "bad\nsubject", "status": "in_progress"}]},
        )
        assert plan == {"done": 0, "total": 1, "subject": ""}

    def test_task_subject_only(self):
        plan = extract_plan("Task", {"description": "spin up a subagent"})
        assert plan == {"subject": "spin up a subagent"}

    def test_non_plan_tool_returns_none(self):
        assert extract_plan("Bash", {"command": "ls"}) is None


class TestElapsedFormat:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0s"),
            (5, "5s"),
            (59, "59s"),
            (60, "1m 00s"),
            (150, "2m 30s"),
            (3600, "1h 00m"),
            (3660, "1h 01m"),
        ],
    )
    def test_format(self, seconds, expected):
        assert format_elapsed(seconds) == expected


class TestLayoutExact:
    def test_status_only(self):
        assert render_summary(goal_line="", status=STATUS_WORKING) == "⚙️ working"

    def test_goal_and_status(self):
        assert render_summary(goal_line="fix bug", status=STATUS_WORKING) == (
            "fix bug\n⚙️ working"
        )

    def test_all_lines(self):
        out = render_summary(
            goal_line="fix bug",
            status=STATUS_WORKING,
            plan_done=1,
            plan_total=3,
            plan_subject="wiring it up",
            activity="running commands",
            elapsed_str="2m 30s",
            open_qs=(4, 6),
        )
        assert out == (
            "fix bug\n"
            "⚙️ working\n"
            "Plan: 1/3 — current: wiring it up\n"
            "Now: running commands — 2m 30s\n"
            "Open questions: Q4, Q6"
        )

    def test_empty_lines_omitted(self):
        # No plan, no activity, no open questions → only goal + status.
        out = render_summary(goal_line="fix bug", status=STATUS_WAITING_REPLY)
        assert out == "fix bug\n⏳ waiting for your reply"

    def test_plan_without_subject_omits_current(self):
        out = render_summary(
            goal_line="", status=STATUS_WORKING, plan_done=2, plan_total=5,
        )
        assert out == "⚙️ working\nPlan: 2/5"

    def test_now_without_elapsed_omits_tail(self):
        out = render_summary(
            goal_line="", status=STATUS_WORKING, activity="reading files",
        )
        assert out == "⚙️ working\nNow: reading files"


# ---------------------------------------------------------------------------
# Authority model (§5).
# ---------------------------------------------------------------------------


class TestAuthorityModel:
    async def test_newer_revision_lowers_rank(self):
        c = _make()
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        assert c._status == STATUS_WAITING_REPLY
        # A LATER answer legitimately drops the rank waiting → working.
        await c.submit_status(STATUS_WORKING, 6)
        assert c._status == STATUS_WORKING

    async def test_older_or_equal_never_overrides(self):
        c = _make()
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        await c.submit_status(STATUS_WORKING, 5)   # equal
        assert c._status == STATUS_WAITING_REPLY
        await c.submit_status(STATUS_WORKING, 3)   # older
        assert c._status == STATUS_WAITING_REPLY

    async def test_late_replayed_frame_cannot_overwrite_newer_waiting(self):
        c = _make()
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        # A stale/late working transition (revision 2) must NOT win.
        await c.submit_status(STATUS_WORKING, 2)
        assert c._status == STATUS_WAITING_REPLY

    async def test_terminal_absolute(self):
        c = _make()
        await c.finalize(STATUS_COMPLETED)
        assert c._status == STATUS_COMPLETED
        await c.submit_status(STATUS_WORKING, 999)
        assert c._status == STATUS_COMPLETED

    async def test_none_revision_rejected(self):
        c = _make()
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        await c.submit_status(STATUS_WORKING, None)
        assert c._status == STATUS_WAITING_REPLY

    async def test_activity_never_changes_status(self):
        c = _make()
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        await c.note_turn_start()  # turn running, but status is set by lifecycle
        await c.submit_activity("running commands")
        assert c._status == STATUS_WAITING_REPLY
        c.shutdown()


# ---------------------------------------------------------------------------
# Throttle + immediate status flush + lifecycle flush (§5).
# ---------------------------------------------------------------------------


class TestThrottleAndFlush:
    async def test_status_change_flushes_immediately_despite_throttle(self):
        seq = FakeSequencer()
        clock = Clock()
        c = _make(seq, clock=clock)
        await c.note_turn_start()
        await c.submit_activity("reading files")  # edit #1 (last_edit=-inf)
        n = len(seq.edits)
        clock.t = 1.0  # only 1s later — inside the 10s throttle window
        await c.submit_status(STATUS_WAITING_REPLY, 5)  # status change → force
        assert len(seq.edits) == n + 1
        assert seq.edits[-1][1].splitlines()[1] == "⏳ waiting for your reply"
        c.shutdown()

    async def test_activity_coalesces_within_throttle_window(self):
        seq = FakeSequencer()
        clock = Clock()
        c = _make(seq, clock=clock)
        await c.note_turn_start()
        await c.submit_activity("reading files")   # edit #1
        clock.t = 2.0
        await c.submit_activity("running commands")  # throttled (2s < 10s)
        clock.t = 5.0
        await c.submit_activity("editing files")     # throttled
        assert len(seq.edits) == 1  # at most one edit per throttle window
        c.shutdown()

    async def test_turn_end_forces_lifecycle_flush(self):
        seq = FakeSequencer()
        clock = Clock()
        c = _make(seq, clock=clock)
        await c.note_turn_start()
        await c.submit_activity("reading files")  # edit #1
        clock.t = 1.0
        await c.note_turn_end()  # mandatory lifecycle flush (force)
        # The Now line is dropped (turn no longer running).
        assert "Now:" not in seq.edits[-1][1]
        assert len(seq.edits) == 2
        c.shutdown()

    async def test_failed_edit_not_cached_as_rendered(self):
        seq = FakeSequencer()
        seq.fail_next = 1
        clock = Clock()
        c = _make(seq, clock=clock)
        await c.note_turn_start()
        await c.submit_status(STATUS_WAITING_REPLY, 5)  # force → FAILED
        assert seq.edits == []            # nothing recorded (the edit failed)
        assert c._last_rendered is None   # not cached → a retry will re-attempt
        c.shutdown()

    async def test_no_edit_without_message_id(self):
        seq = FakeSequencer()
        c = _make(seq, message_id=None)
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        assert seq.edits == []
        c.shutdown()


# ---------------------------------------------------------------------------
# Elapsed tick lifecycle (§5 B1).
# ---------------------------------------------------------------------------


class TestTickLifecycle:
    async def test_tick_starts_on_working_turn(self):
        c = _make()
        await c.note_turn_start()  # default status working + turn running
        assert c._tick_task is not None
        c.shutdown()

    async def test_tick_stops_on_turn_end(self):
        c = _make()
        await c.note_turn_start()
        await c.note_turn_end()
        assert c._tick_task is None

    async def test_tick_stops_when_status_leaves_working(self):
        c = _make()
        await c.note_turn_start()
        assert c._tick_task is not None
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        assert c._tick_task is None
        c.shutdown()

    async def test_tick_stops_on_finalize(self):
        c = _make()
        await c.note_turn_start()
        await c.finalize(STATUS_COMPLETED)
        assert c._tick_task is None

    async def test_tick_advances_elapsed_no_spam(self):
        seq = FakeSequencer()
        clock = Clock()
        c = _make(seq, clock=clock)
        await c.note_turn_start()            # base = 0
        await c.submit_activity("running commands")  # edit @ elapsed 0s
        first = seq.edits[-1][1]
        assert "Now: running commands — 0s" in first
        # One productive tick per 60s.
        clock.t = 60.0
        assert await c._tick_body() is True
        assert "Now: running commands — 1m 00s" in seq.edits[-1][1]
        n = len(seq.edits)
        # A second tick at the SAME clock produces no new edit (elapsed
        # unchanged + throttle) — no edit spam.
        assert await c._tick_body() is True
        assert len(seq.edits) == n
        # Next minute → one more edit.
        clock.t = 120.0
        assert await c._tick_body() is True
        assert "Now: running commands — 2m 00s" in seq.edits[-1][1]
        assert len(seq.edits) == n + 1
        c.shutdown()

    async def test_tick_body_not_eligible_when_not_working(self):
        c = _make()
        await c.note_turn_start()
        await c.submit_status(STATUS_WAITING_REPLY, 5)
        assert await c._tick_body() is False
        c.shutdown()


# ---------------------------------------------------------------------------
# Pin probe / WARN / retry (§5).
# ---------------------------------------------------------------------------


class TestPin:
    async def test_pin_success(self):
        calls = []

        async def pin(mid):
            calls.append(mid)
            return True

        c = _make(pin=pin)
        await c.ensure_pinned()
        assert calls == [500]
        assert c._pinned is True

    async def test_pin_failure_warns_once_then_retries_on_lifecycle_flush(
        self, caplog,
    ):
        results = [False, True]

        async def pin(mid):
            return results.pop(0)

        c = _make(pin=pin)
        with caplog.at_level(logging.WARNING):
            await c.ensure_pinned()          # fails → WARN once
        assert c._pinned is False
        assert c._pin_warned is True
        warns = [r for r in caplog.records if "could not pin" in r.message]
        assert len(warns) == 1
        # A lifecycle (force) flush retries the pin — now succeeds.
        await c.note_turn_end()
        assert c._pinned is True
        c.shutdown()

    async def test_pin_warns_only_once_across_retries(self, caplog):
        async def pin(mid):
            return False

        c = _make(pin=pin)
        with caplog.at_level(logging.WARNING):
            await c.ensure_pinned()
            await c.note_turn_end()
            await c.note_turn_end()
        warns = [r for r in caplog.records if "could not pin" in r.message]
        assert len(warns) == 1
        c.shutdown()

    async def test_no_pin_primitive_is_noop(self):
        c = _make(pin=None)
        await c.ensure_pinned()  # must not raise
        assert c._pinned is False
        c.shutdown()


# ---------------------------------------------------------------------------
# Open-questions rendering from the T3 accessor (§5).
# ---------------------------------------------------------------------------


class TestOpenQuestions:
    async def test_open_questions_rendered_from_accessor(self):
        seq = FakeSequencer()
        nums = [4, 6]
        c = SummaryController(
            engagement_id="e",
            sequencer=seq,
            goal_line="g",
            open_question_numbers=lambda: list(nums),
            message_id=7,
            _now=Clock().now,
            _sleep=_park,
        )
        await c.submit_status(STATUS_WAITING_REPLY, 1)
        assert "Open questions: Q4, Q6" in seq.edits[-1][1]
        c.shutdown()

    async def test_no_open_questions_line_when_empty(self):
        seq = FakeSequencer()
        c = _make(seq, open_qs=())
        await c.submit_status(STATUS_WAITING_REPLY, 1)
        assert "Open questions:" not in seq.edits[-1][1]
        c.shutdown()


# ---------------------------------------------------------------------------
# Waiting-for-approval status copy is available (ask-registry source).
# ---------------------------------------------------------------------------


async def test_waiting_for_approval_copy():
    seq = FakeSequencer()
    c = _make(seq)
    await c.submit_status(STATUS_WAITING_APPROVAL, 1)
    assert seq.edits[-1][1].splitlines()[1] == "🔐 waiting for your approval"
    c.shutdown()


async def test_terminal_copies():
    assert render_summary(goal_line="", status=STATUS_CANCELLED) == "🛑 cancelled"
    assert render_summary(goal_line="", status=STATUS_ERROR) == "⚠️ error"
    assert render_summary(goal_line="", status=STATUS_COMPLETED) == "✅ completed"
