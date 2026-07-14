"""Per-engagement live SUMMARY controller (v0.79.0 §5 — engagement-topic UX).

R1 (operator ruling): the FIRST topic message is a living, pinned SUMMARY;
everything else flows below it as an append-only causal log. This module owns
that one summary message for one ``claude_code`` engagement.

ONE serialized controller (an :class:`asyncio.Lock` guards every state mutation
and flush) owns the summary. Consumers submit DESIRED STATE — status, activity,
plan progress, elapsed base, open questions — and the controller coalesces +
throttles the resulting edits, posting them through the per-topic OUTPUT
SEQUENCER as NON-narration edits (the R1 exception: summary edits never seal the
open narration, are never themselves sealed, and never advance the narration
high-water mark — see :meth:`channels.output_sequencer.OutputSequencer.edit_summary`).

AUTHORITY MODEL (§5, Sol r2-8/r3-3): activity/plan/elapsed frames NEVER submit
status. Only LIFECYCLE sources do — the driver turn lifecycle, ``interaction_state``
and the ask registry — and each acquires a monotonic REVISION from ONE
engagement-wide atomic allocator (persisted with the engagement record) at
transition time, so the sources are totally ordered and collision-free. A NEWER
revision may LOWER the status rank (waiting → working after an answer is
legitimate); an OLDER or EQUAL revision never overrides; a TERMINAL status is
absolute (nothing overrides it once set).

Clocks are injectable (``_now`` / ``_sleep``); no code here patches the global
``asyncio.sleep`` (the module-local / injected-clock rule, CLAUDE.md memory cage).
The elapsed TICK (§5 B1) is THE single sanctioned timer in this design.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from channels.output_sequencer import FAILED
from text_util import is_unsafe_text

logger = logging.getLogger(__name__)

# -- Status copy (EXACT — §5; no parse_mode) --------------------------------
STATUS_WORKING = "⚙️ working"
STATUS_WAITING_REPLY = "⏳ waiting for your reply"
STATUS_WAITING_APPROVAL = "🔐 waiting for your approval"
STATUS_COMPLETED = "✅ completed"
STATUS_CANCELLED = "🛑 cancelled"
STATUS_ERROR = "⚠️ error"

_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_COMPLETED, STATUS_CANCELLED, STATUS_ERROR}
)

# Terminal ``outcome`` (finalize) → status line.
OUTCOME_STATUS: dict[str, str] = {
    "completed": STATUS_COMPLETED,
    "cancelled": STATUS_CANCELLED,
    "error": STATUS_ERROR,
    "failed": STATUS_ERROR,
}

# -- tunables (module-local so tests can shrink them) -----------------------
_THROTTLE_S = 10.0        # §5: edits ≥10s apart EXCEPT status-class changes.
_TICK_S = 60.0            # §5 B1: one edit-eligible elapsed tick per 60s.
_PLAN_SUBJECT_CAP = 60    # §5 B2: 60-char cap on agent-authored plan subjects.


# ---------------------------------------------------------------------------
# Pure helpers (activity mapping, plan extraction, elapsed, rendering).
# ---------------------------------------------------------------------------

# §5 S5: coarse tool_use → activity mapping.
_ACTIVITY_READING = frozenset({"Read", "Glob", "Grep"})
_ACTIVITY_EDITING = frozenset({"Write", "Edit", "NotebookEdit"})
_ACTIVITY_RESEARCH = frozenset({"WebFetch", "WebSearch"})


def activity_for_tool(tool_name: str) -> str:
    """Coarse activity phrase for a ``tool_use`` block (§5 S5).

    Read/Glob/Grep → ``reading files``; Write/Edit/NotebookEdit → ``editing
    files``; Bash → ``running commands``; Task*/TodoWrite → ``planning``;
    WebFetch/WebSearch → ``researching``; ``mcp__<server>__…`` → ``using
    <server> tools``; anything else → ``working``.
    """
    name = tool_name or ""
    if name in _ACTIVITY_READING:
        return "reading files"
    if name in _ACTIVITY_EDITING:
        return "editing files"
    if name == "Bash":
        return "running commands"
    if name.startswith("Task") or name == "TodoWrite":
        return "planning"
    if name in _ACTIVITY_RESEARCH:
        return "researching"
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 and parts[1] else "mcp"
        return f"using {server} tools"
    return "working"


def sanitize_plan_subject(subject: object) -> str:
    """Sanitize an agent-authored plan subject (§5 B2).

    UNSAFE-TEXT (v0.78 predicate — control/bidi codepoints incl. newlines) is
    rejected outright (returns ``""`` so the ``current:`` clause is omitted —
    a subject with a newline would otherwise break the single-line summary
    layout); otherwise the subject is capped at 60 characters.
    """
    if not isinstance(subject, str) or not subject:
        return ""
    if is_unsafe_text(subject):
        return ""
    return subject[:_PLAN_SUBJECT_CAP]


def extract_plan(tool_name: str, tool_input: dict) -> dict | None:
    """Derive a plan-progress DESIRED-STATE fragment from a Task*/TodoWrite
    tool_use payload (§5 B2), or ``None`` when the tool carries no plan.

    ``TodoWrite`` → ``{done, total, subject}`` from its ``todos`` list (done =
    completed count; subject = the in-progress item's text). ``Task*`` → a
    ``{subject}`` fragment only (the sub-agent's description), leaving the
    done/total counts unchanged. ``subject`` fragments are already sanitized.
    """
    name = tool_name or ""
    inp = tool_input if isinstance(tool_input, dict) else {}
    if name == "TodoWrite":
        todos = inp.get("todos")
        if not isinstance(todos, list):
            return None
        total = len(todos)
        done = sum(
            1 for t in todos
            if isinstance(t, dict) and t.get("status") == "completed"
        )
        subject = ""
        for t in todos:
            if isinstance(t, dict) and t.get("status") == "in_progress":
                subject = t.get("activeForm") or t.get("content") or ""
                break
        return {
            "done": done,
            "total": total,
            "subject": sanitize_plan_subject(subject),
        }
    if name.startswith("Task"):
        subject = inp.get("description") or inp.get("prompt") or ""
        return {"subject": sanitize_plan_subject(subject)}
    return None


def format_elapsed(seconds: float) -> str:
    """Human-readable elapsed string (``45s`` / ``2m 30s`` / ``1h 05m``)."""
    total = int(seconds) if seconds and seconds > 0 else 0
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def render_summary(
    *,
    goal_line: str,
    status: str,
    plan_done: int | None = None,
    plan_total: int | None = None,
    plan_subject: str = "",
    activity: str | None = None,
    elapsed_str: str = "",
    open_qs: tuple[int, ...] = (),
) -> str:
    """Render the EXACT summary layout (§5; no parse_mode, omit empty lines).

    ::

        <goal line>
        <status line>
        Plan: <done>/<total> — current: <subject>
        Now: <activity> — <elapsed>
        Open questions: Q<n>, Q<m>

    The goal line (from the engagement's topic-name string source) is the stable
    header; the status line follows; the Plan / Now / Open-questions lines are
    each omitted when empty.
    """
    lines: list[str] = []
    if goal_line:
        lines.append(goal_line)
    lines.append(status)
    if plan_total:
        subj = f" — current: {plan_subject}" if plan_subject else ""
        lines.append(f"Plan: {plan_done or 0}/{plan_total}{subj}")
    if activity:
        tail = f" — {elapsed_str}" if elapsed_str else ""
        lines.append(f"Now: {activity}{tail}")
    if open_qs:
        lines.append(
            "Open questions: " + ", ".join(f"Q{n}" for n in open_qs)
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The controller.
# ---------------------------------------------------------------------------


async def _default_sleep(seconds: float) -> None:  # pragma: no cover - trivial
    await asyncio.sleep(seconds)


class SummaryController:
    """One serialized live-summary controller for one engagement (§5)."""

    def __init__(
        self,
        *,
        engagement_id: str,
        sequencer,
        goal_line: str,
        open_question_numbers: Callable[[], list[int]] = lambda: [],
        pin_message: Callable[[int], Awaitable[bool]] | None = None,
        message_id: int | None = None,
        _now: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], Awaitable[None]] | None = None,
        throttle_s: float = _THROTTLE_S,
        tick_s: float = _TICK_S,
    ) -> None:
        self.engagement_id = engagement_id
        self._sequencer = sequencer
        self._goal_line = goal_line
        self._open_question_numbers = open_question_numbers
        self._pin_message = pin_message
        self._message_id = message_id
        self._now = _now
        self._sleep = _sleep or _default_sleep
        self._throttle_s = throttle_s
        self._tick_s = tick_s

        self._lock = asyncio.Lock()
        # DESIRED STATE.
        self._status = STATUS_WORKING
        # ``_status_rev`` is the revision at which the current status was set; a
        # submission wins iff its revision is strictly greater (older/equal never
        # overrides). Starts below zero so the first revision-0 lifecycle
        # submission wins.
        self._status_rev = -1
        self._activity: str | None = None
        self._plan_done: int | None = None
        self._plan_total: int | None = None
        self._plan_subject = ""
        self._turn_running = False
        self._turn_base: float | None = None  # monotonic base for elapsed
        # Edit throttle + last-rendered (coalescing / no-op).
        self._last_edit_ts = float("-inf")
        self._last_rendered: str | None = None
        # Pin state (best-effort; retried on lifecycle flush).
        self._pinned = False
        self._pin_warned = False
        # The SINGLE sanctioned timer (§5 B1).
        self._tick_task: asyncio.Task | None = None

    # -- message-id adoption ------------------------------------------------
    def adopt_message_id(self, message_id: int | None) -> None:
        """Bind the summary Telegram message id (posted at boot, persisted in
        the engagement record; a resumed engagement adopts it on attach)."""
        self._message_id = message_id

    @property
    def message_id(self) -> int | None:
        return self._message_id

    # -- lifecycle status (authority-checked) -------------------------------
    async def submit_status(self, status: str, revision: int | None) -> None:
        """Submit a lifecycle STATUS at *revision* (§5 authority model).

        Accepted iff the current status is not terminal AND *revision* is
        strictly greater than the revision the current status was set at. A
        status-class CHANGE flushes immediately; a same-status revision bump is
        a silent no-op.
        """
        async with self._lock:
            if self._status in _TERMINAL_STATUSES:
                return  # terminal absolute
            if revision is None or revision <= self._status_rev:
                return  # older/equal never overrides
            changed = status != self._status
            self._status = status
            self._status_rev = revision
            self._reconcile_tick_locked()
            # §5: status-class changes flush immediately; a same-status bump
            # touches nothing visible, so no flush is needed.
            if changed:
                await self._flush_locked(force=True)

    async def finalize(self, status: str) -> None:
        """Set the TERMINAL status (§5 — terminal absolute), cancel the tick and
        perform the mandatory engagement-finalize flush. Ignores the revision
        ordering: a terminal status wins unconditionally and, once set,
        :meth:`submit_status` rejects every later submission."""
        async with self._lock:
            self._status = status
            self._turn_running = False
            self._cancel_tick_locked()
            await self._flush_locked(force=True)

    # -- activity / plan (never status) -------------------------------------
    async def submit_activity(self, activity: str) -> None:
        """Submit the current ACTIVITY (§5 S5). DESIRED-STATE coalescing: the
        edit is throttled, so alternating tools yield at most one edit per
        throttle window (rate limiting, not just dedup)."""
        async with self._lock:
            self._activity = activity
            await self._flush_locked(force=False)

    async def submit_plan(
        self,
        *,
        done: int | None = None,
        total: int | None = None,
        subject: str | None = None,
    ) -> None:
        """Merge a plan-progress fragment (§5 B2). ``None`` fields are left
        unchanged (a Task* subject update keeps the last TodoWrite counts)."""
        async with self._lock:
            if done is not None:
                self._plan_done = done
            if total is not None:
                self._plan_total = total
            if subject is not None:
                self._plan_subject = subject
            await self._flush_locked(force=False)

    # -- turn lifecycle (elapsed base + tick) -------------------------------
    async def note_turn_start(self) -> None:
        """A CLI turn started: reset the elapsed base and (re)start the tick if
        the status is working."""
        async with self._lock:
            self._turn_running = True
            self._turn_base = self._now()
            self._reconcile_tick_locked()

    async def note_turn_end(self) -> None:
        """A CLI turn ended: stop the tick and perform the mandatory
        turn-end lifecycle flush (§5)."""
        async with self._lock:
            self._turn_running = False
            self._reconcile_tick_locked()
            await self._flush_locked(force=True)

    # -- pin (best-effort) --------------------------------------------------
    async def ensure_pinned(self) -> None:
        """Attempt to pin the summary (best-effort; §5). Retried on every
        lifecycle flush until it succeeds."""
        async with self._lock:
            await self._maybe_pin_locked()

    async def _maybe_pin_locked(self) -> None:
        if self._pin_message is None or self._message_id is None or self._pinned:
            return
        try:
            ok = bool(await self._pin_message(self._message_id))
        except Exception as exc:  # noqa: BLE001 — pin is best-effort
            ok = False
            logger.debug("summary pin raised (engagement %s): %s",
                         self.engagement_id, exc)
        if ok:
            self._pinned = True
        elif not self._pin_warned:
            self._pin_warned = True
            logger.warning(
                "engagement %s: could not pin the live summary message "
                "(best-effort; will retry on the next lifecycle flush)",
                self.engagement_id,
            )

    # -- tick (THE single sanctioned timer — §5 B1) -------------------------
    def _tick_should_run(self) -> bool:
        return self._status == STATUS_WORKING and self._turn_running

    def _reconcile_tick_locked(self) -> None:
        """Start/stop the elapsed tick to match the working+turn-running
        predicate (§5 B1). Caller holds the lock."""
        if self._tick_should_run():
            if self._tick_task is None or self._tick_task.done():
                self._tick_task = asyncio.create_task(self._tick_loop())
        else:
            self._cancel_tick_locked()

    def _cancel_tick_locked(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            self._tick_task = None

    async def _tick_body(self) -> bool:
        """One elapsed-refresh tick (§5 B1): submit ONLY an elapsed refresh
        (never status), routed through the same throttle/no-op gate. Returns
        ``False`` when no longer eligible (working+turn-running lapsed)."""
        async with self._lock:
            if not self._tick_should_run():
                return False
            await self._flush_locked(force=False)
            return True

    async def _tick_loop(self) -> None:  # pragma: no cover - exercised via _tick_body
        """THE single sanctioned timer in this design (§5 B1): one edit-eligible
        elapsed tick per ``tick_s`` (60s) while working and a turn is running.
        Uses the INJECTED ``_sleep`` — never the global ``asyncio.sleep`` (the
        module-local / injected-clock rule, CLAUDE.md memory cage)."""
        try:
            while True:
                await self._sleep(self._tick_s)
                if not await self._tick_body():
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "summary elapsed tick error (engagement %s): %s",
                self.engagement_id, exc,
            )

    def shutdown(self) -> None:
        """Cancel the tick (driver teardown). Idempotent."""
        self._cancel_tick_locked()

    # -- render + flush -----------------------------------------------------
    def _render_locked(self) -> str:
        elapsed_str = ""
        activity = None
        # Elapsed + the Now line are shown ONLY while working (a turn is
        # actively running); between turns / waiting there is no live activity.
        if self._status == STATUS_WORKING and self._turn_running and self._activity:
            activity = self._activity
            if self._turn_base is not None:
                elapsed_str = format_elapsed(self._now() - self._turn_base)
        open_qs = tuple(self._open_question_numbers() or ())
        return render_summary(
            goal_line=self._goal_line,
            status=self._status,
            plan_done=self._plan_done,
            plan_total=self._plan_total,
            plan_subject=self._plan_subject,
            activity=activity,
            elapsed_str=elapsed_str,
            open_qs=open_qs,
        )

    async def _flush_locked(self, *, force: bool) -> None:
        """Coalesce → throttle → edit (caller holds the lock).

        Edits are ≥``throttle_s`` apart EXCEPT when *force* (a status-class
        change or a mandatory lifecycle flush). Routes through the sequencer's
        NON-narration ``edit_summary`` (F1 no-op gate lives there too). On a
        FORCE flush the best-effort pin is retried (§5)."""
        if self._message_id is not None:
            text = self._render_locked()
            now = self._now()
            due = force or (now - self._last_edit_ts) >= self._throttle_s
            if due and text != self._last_rendered:
                res = await self._sequencer.edit_summary(self._message_id, text)
                self._last_edit_ts = now
                if res != FAILED:
                    self._last_rendered = text
        if force:
            await self._maybe_pin_locked()
