"""Engagement primitive — Tier 2 Specialist interactive mode + (Plan 3+) Tier 3 Executors.

Symmetric with :mod:`specialist_registry`. Owns:
- EngagementRecord (one in-flight engagement)
- EngagementRegistry (in-memory dict + ``/data/engagements.json``: in-flight
  records for crash recovery PLUS terminal tombstones, which age out after
  ``_TERMINAL_RETENTION_DAYS`` — D-4, v0.69.0)
- Idle sweep (fires ``idle_detected`` bus events + session-suspends live clients)
- Orphan recovery (startup: load the file; "active" rows are reconciled to
  idle — no driver survives a restart — and remain dormant until the next
  user turn in their topic; ``tools.reap_stale_engagements`` retires them
  after the reap TTL)

See docs/superpowers/specs/2026-04-22-3.5-plan2-engagement-primitive-design.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sweep constants
# ---------------------------------------------------------------------------

_IDLE_REMINDER_DAYS_SPECIALIST = 3
_IDLE_REMINDER_DAYS_EXECUTOR = 7          # default; per-type override lands Plan 3
_IDLE_REMINDER_REFIRE_DAYS = 7
_SESSION_SUSPEND_IDLE_S = 86400
_IDLE_SWEEP_CRON = "0 8 * * *"            # daily 08:00 user TZ
# D-4 (v0.69.0): terminal tombstones stay on disk this long, then age out of
# the snapshot on the next write — bounds the file while keeping the P32
# duplicate-task guard and post-mortems working across restarts.
_TERMINAL_RETENTION_DAYS = 30

# v0.79.0 (§3): sentinel for the strict terminal transition's full-field
# snapshot — distinguishes "origin had no such key" from "key was None" so the
# rollback can DELETE a key the transition added rather than leaving it None.
_FIELD_MISSING = object()


def _restore_origin_field(rec: "EngagementRecord", key: str, snapped: Any) -> None:
    """Restore ``rec.origin[key]`` to a strict-transition snapshot value.

    ``_FIELD_MISSING`` means the key was ABSENT before the transition — the
    rollback removes it rather than resurrecting it as ``None``."""
    if snapped is _FIELD_MISSING:
        rec.origin.pop(key, None)
    else:
        rec.origin[key] = snapped


# ---------------------------------------------------------------------------
# W2/Sol B9 (Task 7) — interaction_state pure transition core.
# ---------------------------------------------------------------------------
#
# Transition table (normative — design §W2, Sol r3-B4):
#   first_contact      : first_contact_required -> awaiting_operator
#   operator_answered  : {first_contact_required, awaiting_operator} -> authorized
#   operator_turn      : {first_contact_required, awaiting_operator} -> authorized
#   anything else (including from "authorized", from "" (not
#   interaction-required), or an event not valid from the current state)
#   -> no-op (None). Never backwards.


def _pure_interaction_transition(current: str, event: str) -> str | None:
    """Compute the next ``interaction_state``, or ``None`` for a no-op.

    Pure (no I/O, no locking) so it's trivially unit-testable and reusable
    by the atomic locked mutator below.
    """
    if event == "first_contact":
        return "awaiting_operator" if current == "first_contact_required" else None
    if event in ("operator_answered", "operator_turn"):
        if current in ("first_contact_required", "awaiting_operator"):
            return "authorized"
        return None
    return None


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


@dataclass
class EngagementRecord:
    """One in-flight engagement.

    ``kind`` = "specialist" for Tier 2 interactive mode; "executor" for Tier 3
    (Plan 3+). ``role_or_type`` is the specialist role (e.g. "finance") or the
    executor type (e.g. "configurator").

    ``status`` transitions:
      active ──first idle sweep past 24h──▶ idle
      active ──registry load after restart──▶ idle   (D-4 boot reconcile)
      idle    ──next user turn──▶ active
      active  ──emit_completion / /complete──▶ completed
      active  ──/cancel / cancel_engagement──▶ cancelled
      active/idle ──reap sweep past ENGAGEMENT_REAP_DAYS──▶ cancelled  (D-4)
      active  ──resume twice failed / sweep orphan──▶ error
    """

    id: str
    kind: str
    role_or_type: str
    driver: str
    status: str
    topic_id: int | None
    started_at: float
    last_user_turn_ts: float
    last_idle_reminder_ts: float
    completed_at: float | None
    sdk_session_id: str | None
    origin: dict[str, Any]
    task: str
    # E-12 (v0.37.0): channel-side state for in-place edits across restarts.
    pinned_message_id: int | None = None
    progress_message_id: int | None = None
    current_state_emoji: str | None = None
    # C-1 v0.37.2: snapshot of executor's tools.allowed at engagement
    # creation. Drives the engagement_permission_relay hook (spec §3.5).
    tools_allowed: tuple[str, ...] = ()
    # G-1 v0.37.7: snapshot of executor's permission_mode at engagement
    # creation. When "auto" or "bypassPermissions" the relay hook
    # short-circuits without surfacing a permission keyboard.
    permission_mode: str = "acceptEdits"
    # §3.8: immutable snapshot of the resolved plugin artifacts this
    # engagement launched with — each {"name","artifact_id","path"}. Boot
    # replay renders --plugin-dir flags from THESE recorded paths, never a
    # re-resolution of current assignments. Preserved by every rewrite.
    plugin_artifacts: tuple[dict, ...] = ()
    # W2/Sol B9 (Task 7): observational turn-taking state. "" (default) =
    # not interaction-required (most engagements). Interaction-required
    # engagements start at "first_contact_required" (set by engage_executor
    # at create — Task 8) and advance via ``advance_interaction_state``:
    # first_contact_required -> awaiting_operator -> authorized. Never
    # backwards; see ``_pure_interaction_transition``.
    interaction_state: str = ""
    # Task 6 (spec §4.6): the concurrency Permit this interactive
    # specialist delegation holds, if any (set by tools.py's
    # delegate_to_agent right after `create()`, None for executor
    # engagements — they never acquire one). NOT persisted to the
    # tombstone (`_write_tombstone_locked` below lists fields explicitly)
    # — a live Permit cannot survive a restart; concurrency state is
    # memory-only and resets with the process. Released exactly once by
    # `_finalize_engagement` (the shared completion/cancel/reap funnel)
    # or, for a pre-finalize failure (topic/driver-start), inline at the
    # point of failure — see delegate_to_agent's interactive branch.
    permit: Any = None
    # v0.79.0 (§4): persisted question numbering. ``next_question_number`` is a
    # monotonic per-engagement allocator (never rewound, even when a question
    # closes) so every displayed ``Q<n>`` is durable and unique across restarts.
    # ``open_questions`` is the set of still-open (unsettled) questions, each a
    # ``{"n": int, "tg_message_id": int|None}`` dict — boot reconciliation
    # settles any entry whose broker record did not survive the restart.
    next_question_number: int = 1
    open_questions: tuple[dict, ...] = ()
    # v0.79.0 (§5): the pinned live-summary controller state. ``summary_message_id``
    # is the Telegram id of the first (pinned) topic message, posted at boot
    # BEFORE the subprocess starts so a resumed engagement adopts it on attach.
    # ``summary_revision`` is the engagement-wide monotonic revision allocator —
    # every lifecycle status transition acquires the next revision here (totally
    # ordered, collision-free), so a newer revision may lower the status rank
    # while an older/equal one never overrides.
    summary_message_id: int | None = None
    summary_revision: int = 0
    # W-R6 (v0.81.0): the persisted SHORT topic title (2-3 words). Set once at
    # engage_executor ingest (engager-supplied ``topic_title`` normalized, or a
    # Casa-derived fallback from the brief/task), then read by BOTH the
    # topic-name state edit (telegram.update_topic_state) and the live-summary
    # title (claude_code_driver._summary_goal_line) — a single durable source.
    # Additive + absent-tolerant on load (legacy rows have no key → "" → each
    # reader falls back to the derived concise_task label, no crash).
    topic_title: str = ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class EngagementRegistry:
    """In-memory dict + ``/data/engagements.json`` tombstone.

    All mutation methods acquire ``self._lock`` and write the tombstone
    inside the lock, same pattern as SpecialistRegistry.register_delegation.
    The ``bus`` parameter is used to publish ``idle_detected`` events from
    the sweep task; tests that don't exercise the sweep may pass ``None``.
    """

    def __init__(self, *, tombstone_path: str, bus: Any | None) -> None:
        self._tombstone_path = tombstone_path
        self._bus = bus
        self._records: dict[str, EngagementRecord] = {}
        self._topic_index: dict[int, str] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Read the tombstone into memory. Called once at startup."""
        if not os.path.exists(self._tombstone_path):
            return
        try:
            with open(self._tombstone_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "Engagement tombstone corrupt or unreadable (%s): %s — truncating",
                self._tombstone_path, exc,
            )
            try:
                with open(self._tombstone_path, "w", encoding="utf-8") as fh:
                    json.dump([], fh)
            except OSError:
                pass
            return
        if not isinstance(raw, list):
            logger.error(
                "Engagement tombstone %s is not a JSON array; truncating",
                self._tombstone_path,
            )
            try:
                with open(self._tombstone_path, "w", encoding="utf-8") as fh:
                    json.dump([], fh)
            except OSError:
                pass
            return
        reconciled_any = False
        for row in raw:
            try:
                rec = EngagementRecord(
                    id=row["id"],
                    kind=row["kind"],
                    role_or_type=row["role_or_type"],
                    driver=row["driver"],
                    status=row["status"],
                    topic_id=row.get("topic_id"),
                    started_at=float(row["started_at"]),
                    last_user_turn_ts=float(row["last_user_turn_ts"]),
                    last_idle_reminder_ts=float(row.get("last_idle_reminder_ts", 0.0)),
                    completed_at=row.get("completed_at"),
                    sdk_session_id=row.get("sdk_session_id"),
                    origin=dict(row.get("origin") or {}),
                    task=row.get("task", ""),
                    pinned_message_id=row.get("pinned_message_id"),
                    progress_message_id=row.get("progress_message_id"),
                    current_state_emoji=row.get("current_state_emoji"),
                    tools_allowed=tuple(row.get("tools_allowed") or ()),
                    permission_mode=row.get("permission_mode") or "acceptEdits",
                    plugin_artifacts=tuple(row.get("plugin_artifacts") or ()),
                    interaction_state=row.get("interaction_state") or "",
                    next_question_number=int(row.get("next_question_number", 1) or 1),
                    open_questions=tuple(
                        dict(q) for q in (row.get("open_questions") or ())
                    ),
                    summary_message_id=row.get("summary_message_id"),
                    summary_revision=int(row.get("summary_revision", 0) or 0),
                    topic_title=row.get("topic_title", "") or "",
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping malformed engagement row: %s", exc)
                continue
            # D-4 boot reconcile (v0.69.0): a record loaded as "active" claims
            # a live driver, but no driver survives a restart — the process
            # that ran it died with the old container. Idle is the truthful
            # state: dormant, resumable on the next user turn in its topic
            # (update_user_turn flips it back), and visible to the reap sweep.
            if rec.status == "active":
                rec.status = "idle"
                reconciled_any = True
                logger.info(
                    "boot reconcile: engagement %s active→idle "
                    "(no driver survives a restart)", rec.id[:8],
                )
            self._records[rec.id] = rec
            if rec.topic_id is not None:
                self._topic_index[rec.topic_id] = rec.id

        # v0.69.6: persist the reconcile so the on-disk tombstone matches the
        # in-memory state immediately after boot. Without this the file kept
        # showing "active" until the next mutation, and the disk-reading
        # auditor (invariant E) saw the stale status. Only write when
        # something actually changed (no needless boot churn). load() runs
        # single-threaded during init, but take the lock for consistency with
        # every other tombstone write.
        if reconciled_any:
            async with self._lock:
                await self._write_tombstone_locked()

    def active_and_idle(self) -> list[EngagementRecord]:
        return [r for r in self._records.values() if r.status in ("active", "idle")]

    def terminal_records(self) -> list[EngagementRecord]:
        """v0.79.0 (§3): terminal records, for the boot spool-reconciliation
        owner (drains inbound spools still holding pending receipts/notices)."""
        return [
            r for r in self._records.values()
            if r.status in ("completed", "cancelled", "error")
        ]

    def get(self, engagement_id: str) -> EngagementRecord | None:
        return self._records.get(engagement_id)

    def by_topic_id(self, topic_id: int) -> EngagementRecord | None:
        rec_id = self._topic_index.get(topic_id)
        return self._records.get(rec_id) if rec_id else None

    def recent_for_origin(
        self,
        *,
        channel: str,
        chat_id: str,
        max_age_s: float,
        now: float | None = None,
    ) -> EngagementRecord | None:
        """P32 (v0.37.10): return the most-recent engagement (by
        ``started_at``) for this ``(channel, chat_id)`` started within
        the last ``max_age_s`` seconds, regardless of status.

        Includes completed / cancelled / errored engagements: they stay
        in ``_records`` for the process lifetime and (since D-4,
        v0.69.0) persist on disk as tombstones for
        ``_TERMINAL_RETENTION_DAYS``, so the guard also holds across
        restarts. The duplicate-task guard at the ``engage_executor``
        call site uses this to refuse spawns that overlap with whichever
        task was spawned last.

        ``chat_id`` is coerced via ``str()`` for the compare; channel
        adapters may store the value as int (telegram) or str.
        """
        if now is None:
            now = time.time()
        cutoff = now - max_age_s
        candidates: list[EngagementRecord] = []
        for rec in self._records.values():
            if rec.origin.get("channel", "") != channel:
                continue
            if str(rec.origin.get("chat_id", "")) != chat_id:
                continue
            if rec.started_at < cutoff:
                continue
            candidates.append(rec)
        if not candidates:
            return None
        candidates.sort(key=lambda r: r.started_at, reverse=True)
        return candidates[0]

    # -- Persist helper ---------------------------------------------------

    async def _write_tombstone_locked(self, *, strict: bool = False) -> None:
        """Caller MUST hold self._lock.

        Terminal records are persisted as real tombstones (D-4, v0.69.0) —
        they used to be silently dropped, so the P32 duplicate-task guard
        forgot recent spawns across restarts and the file never matched its
        name. Tombstones age out after ``_TERMINAL_RETENTION_DAYS`` to bound
        the file.

        ``strict`` (B3, Sol r1): when True, a persistence failure PROPAGATES
        instead of being swallowed — used only by
        ``advance_interaction_state``, where returning the new state while the
        authorization never reached disk lets the telegram callback commit an
        ask that a restart would then un-authorize. All other callers keep the
        best-effort warn-and-continue semantics (strict=False).
        """
        cutoff = time.time() - _TERMINAL_RETENTION_DAYS * 86400
        snapshot = []
        for rec in self._records.values():
            if (rec.status in ("completed", "cancelled", "error")
                    and rec.completed_at is not None
                    and rec.completed_at < cutoff):
                continue
            snapshot.append({
                "id": rec.id,
                "kind": rec.kind,
                "role_or_type": rec.role_or_type,
                "driver": rec.driver,
                "status": rec.status,
                "topic_id": rec.topic_id,
                "started_at": rec.started_at,
                "last_user_turn_ts": rec.last_user_turn_ts,
                "last_idle_reminder_ts": rec.last_idle_reminder_ts,
                "completed_at": rec.completed_at,
                "sdk_session_id": rec.sdk_session_id,
                "origin": dict(rec.origin),
                "task": rec.task,
                "pinned_message_id": rec.pinned_message_id,
                "progress_message_id": rec.progress_message_id,
                "current_state_emoji": rec.current_state_emoji,
                "tools_allowed": list(rec.tools_allowed),
                "permission_mode": rec.permission_mode,
                "plugin_artifacts": [dict(pa) for pa in rec.plugin_artifacts],
                "interaction_state": rec.interaction_state,
                "next_question_number": rec.next_question_number,
                "open_questions": [dict(q) for q in rec.open_questions],
                "summary_message_id": rec.summary_message_id,
                "summary_revision": rec.summary_revision,
                "topic_title": rec.topic_title,
            })
        try:
            await asyncio.to_thread(self._write_tombstone, snapshot)
        except Exception as exc:
            if strict:
                raise
            logger.warning("Failed to persist engagement tombstone: %s", exc)

    def _write_tombstone(self, snapshot: list[dict[str, Any]]) -> None:
        # Atomic (temp-file + fsync + os.replace): a crash mid-write must not
        # lose all in-flight engagement state to a truncated tombstone (M15).
        atomic_write_json(self._tombstone_path, snapshot, indent=2)

    # -- Mutators ---------------------------------------------------------

    async def create(
        self,
        kind: str,
        role_or_type: str,
        driver: str,
        task: str,
        origin: dict[str, Any],
        topic_id: int | None,
        tools_allowed: tuple[str, ...] | list[str] = (),
        permission_mode: str = "acceptEdits",
        plugin_artifacts: tuple[dict, ...] | list[dict] = (),
        interaction_state: str = "",
        topic_title: str = "",
    ) -> EngagementRecord:
        engagement_id = uuid.uuid4().hex
        now = time.time()
        rec = EngagementRecord(
            id=engagement_id,
            kind=kind,
            role_or_type=role_or_type,
            driver=driver,
            status="active",
            topic_id=topic_id,
            started_at=now,
            last_user_turn_ts=now,
            last_idle_reminder_ts=0.0,
            completed_at=None,
            sdk_session_id=None,
            origin=dict(origin),
            task=task,
            tools_allowed=tuple(tools_allowed),
            permission_mode=permission_mode or "acceptEdits",
            plugin_artifacts=tuple(dict(pa) for pa in plugin_artifacts),
            interaction_state=interaction_state,
            topic_title=topic_title,
        )
        async with self._lock:
            self._records[engagement_id] = rec
            if topic_id is not None:
                self._topic_index[topic_id] = engagement_id
            await self._write_tombstone_locked()
        logger.info(
            "Engagement %s created (kind=%s role_or_type=%s topic_id=%s)",
            engagement_id[:8], kind, role_or_type, topic_id,
        )
        return rec

    @staticmethod
    def _release_permit(rec: "EngagementRecord") -> None:
        """Task 6 (spec §4.6): release the specialist concurrency permit this
        record holds, if any. Called synchronously by EVERY terminal
        transition below (right after the status change, BEFORE tombstone
        I/O) so a leaked permit can never outlive its engagement — including
        direct ``mark_error`` routes (resume/orphan failures in
        channels/telegram.py) that bypass ``_finalize_engagement``.
        ``Permit.release()`` is idempotent, so ``_finalize_engagement``'s
        own release (and re-entrant terminal calls) are safe no-ops.
        Executor engagements carry ``permit=None`` → guarded no-op."""
        permit = getattr(rec, "permit", None)
        if permit is not None:
            try:
                permit.release()
            except Exception:  # noqa: BLE001 — a bookkeeping release must never break a terminal transition
                logger.warning("engagement %s permit release raised",
                               rec.id[:8], exc_info=True)

    async def mark_completed(self, engagement_id: str, completed_at: float) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.status = "completed"
            rec.completed_at = completed_at
            self._release_permit(rec)
            await self._write_tombstone_locked()

    async def mark_cancelled(self, engagement_id: str) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.status = "cancelled"
            rec.completed_at = time.time()
            self._release_permit(rec)
            await self._write_tombstone_locked()

    async def mark_error(self, engagement_id: str, kind: str, message: str) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.status = "error"
            rec.completed_at = time.time()
            rec.origin["error_kind"] = kind
            rec.origin["error_message"] = message
            self._release_permit(rec)
            await self._write_tombstone_locked()

    async def try_transition_terminal(
        self,
        engagement_id: str,
        outcome: str,  # "completed" | "cancelled" | "error"
        *,
        completed_at: float | None = None,
        error_kind: str = "",
        error_message: str = "",
        stale_before: float | None = None,
        strict: bool = False,
    ) -> bool:
        """Atomically move a record to a terminal status. Returns True only
        for the first caller; False if missing or already terminal.

        L75/L24: emit_completion's fast-path terminal check and
        _finalize_engagement's registry write are separated by real
        suspension points (e.g. a forced-reload await), so a concurrent
        /cancel can race between them. This method is the single
        authoritative gate — only the first caller to flip the record
        terminal may run finalize side effects (topic close, DelegationComplete
        NOTIFICATION, summary retain).

        ``stale_before`` (reap, v0.69.6): win ONLY if ``last_user_turn_ts`` is
        still older than the cutoff. The reap checks staleness before this
        call at a suspension point away; without this guard a user turn that
        revives the record in that window would still be reaped.

        ``strict`` (v0.79.0 §3, Sol r6-2/r7-2): the finalize path uses STRICT
        transactional persistence. Non-strict callers keep the historical
        best-effort behavior (a tombstone write failure is swallowed and the
        in-memory flip stands, which could leave a closed topic with no
        terminal record for boot reconciliation to find). Strict snapshots
        EVERY field the transition mutates (status, completed_at, and the
        error metadata on ``origin``) and, on tombstone-write failure, restores
        the FULL snapshot and re-raises — so a persistence failure leaves the
        record exactly as it was (live), never a memory/disk split. The
        mutate+persist runs under a shield-and-await (mirroring
        ``advance_interaction_state``) so cancellation during ``to_thread``
        cannot tear the pair.
        """
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None or rec.status in ("completed", "cancelled", "error"):
                return False
            if stale_before is not None and rec.last_user_turn_ts >= stale_before:
                # Revived since the reap snapshot — never cancel a live engagement.
                return False
            new_status = (
                outcome if outcome in ("completed", "cancelled") else "error"
            )
            new_completed = (
                completed_at if completed_at is not None else time.time()
            )
            if not strict:
                rec.status = new_status
                rec.completed_at = new_completed
                if new_status == "error":
                    rec.origin["error_kind"] = error_kind or "emit_completion_error"
                    rec.origin["error_message"] = error_message
                # Task 6 (spec §4.6): release the interactive delegation's
                # concurrency permit on this terminal transition (no-op for
                # executor engagements, permit=None). Safe before the write:
                # the non-strict path has no rollback, so the record is
                # committed-terminal in memory regardless of persist outcome.
                self._release_permit(rec)
                await self._write_tombstone_locked()
                return True

            # STRICT: full-field snapshot + shield-and-await + rollback-on-fail.
            snap_status = rec.status
            snap_completed = rec.completed_at
            snap_error_kind = rec.origin.get("error_kind", _FIELD_MISSING)
            snap_error_message = rec.origin.get("error_message", _FIELD_MISSING)

            def _restore() -> None:
                rec.status = snap_status
                rec.completed_at = snap_completed
                _restore_origin_field(rec, "error_kind", snap_error_kind)
                _restore_origin_field(rec, "error_message", snap_error_message)

            async def _mutate_and_persist() -> bool:
                rec.status = new_status
                rec.completed_at = new_completed
                if new_status == "error":
                    rec.origin["error_kind"] = error_kind or "emit_completion_error"
                    rec.origin["error_message"] = error_message
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    _restore()
                    raise
                # Task 6 (spec §4.6): release the permit ONLY after the
                # terminal status is durably committed — the strict path can
                # roll the status back to live on a persist failure, and
                # releasing a still-live engagement's permit would free its
                # scope slot while the interactive specialist is still running.
                self._release_permit(rec)
                return True

            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    # Let the inner mutate+persist (and, on failure, the
                    # rollback) finish under the lock before honoring the
                    # cancel — never a torn memory/disk pair.
                    await asyncio.gather(task, return_exceptions=True)
                raise

    async def mark_idle(self, engagement_id: str) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.status = "idle"
            await self._write_tombstone_locked()

    async def update_user_turn(self, engagement_id: str, ts: float) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.last_user_turn_ts = ts
            # C-fix (2026-05-29): reset the idle-reminder debounce so the next
            # reminder tracks *activity* (the N-day-since-last-turn threshold)
            # rather than the 7-day-since-last-reminder refire clock. Without
            # this, a re-engaged specialist (3 d threshold < 7 d refire) gets
            # its second reminder a few days late. See current-state-spec D7.
            rec.last_idle_reminder_ts = 0.0
            if rec.status == "idle":
                rec.status = "active"
            await self._write_tombstone_locked()

    async def update_last_idle_reminder(self, engagement_id: str, ts: float) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.last_idle_reminder_ts = ts
            await self._write_tombstone_locked()

    async def persist_session_id(self, engagement_id: str, session_id: str) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.sdk_session_id = session_id
            await self._write_tombstone_locked()

    async def set_channel_state(
        self,
        engagement_id: str,
        *,
        pinned_message_id: int | None = None,
        progress_message_id: int | None = None,
        current_state_emoji: str | None = None,
    ) -> None:
        """E-12 (v0.37.0): update the channel-state subset on a record.

        Each kwarg is applied only if not None; omitting an arg leaves the
        current value untouched. Unknown ``engagement_id`` is a no-op (matches
        the other mutators' tolerance for stale callers).
        """
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            if pinned_message_id is not None:
                rec.pinned_message_id = pinned_message_id
            if progress_message_id is not None:
                rec.progress_message_id = progress_message_id
            if current_state_emoji is not None:
                rec.current_state_emoji = current_state_emoji
            await self._write_tombstone_locked()

    async def advance_interaction_state(
        self, engagement_id: str, event: str,
    ) -> str | None:
        """W2/Sol B9 (Task 7): atomic compare-and-set on ``interaction_state``.

        Read record -> compute the pure transition -> write field +
        persist, all under ``self._lock`` so two coroutines racing the same
        event on the same record resolve to exactly one transition (the
        second sees the already-advanced state and gets a no-op). Returns
        the new state, or ``None`` for an unknown engagement or a no-op
        transition (never backwards — see ``_pure_interaction_transition``).
        """
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return None
            new_state = _pure_interaction_transition(rec.interaction_state, event)
            if new_state is None:
                return None
            # B3 (Sol r1): persist STRICTLY and roll back on failure — the
            # telegram callback commits the ask on a successful return, so the
            # authorization MUST have reached disk before we report the new
            # state. On a write failure the callback's `except` path
            # abort_claims + "please tap again" (verified end-to-end by
            # test_telegram_inline_callback).
            prev_state = rec.interaction_state

            async def _mutate_and_persist() -> str:
                rec.interaction_state = new_state
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    rec.interaction_state = prev_state
                    raise
                return new_state

            # B4 (Sol diff r2): SHIELD the mutate+persist so cancelling the
            # CALLER (e.g. the telegram callback task) cannot tear the pair.
            # The inner task runs to completion UNDER THE LOCK — on cancel we
            # await it to completion BEFORE re-raising, so the durable write
            # (and, on failure, the rollback) always finishes while we still
            # hold the lock. Without this a CancelledError mid-``to_thread``
            # left the request armed-then-aborted despite disk authorization
            # (expiring ``no_answer`` on an answered ask). The callback treats
            # a cancellation-after-authorization as committable.
            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    # Retrieve any inner exception (already rolled back) so it
                    # is not flagged "never retrieved"; then honor the cancel.
                    await asyncio.gather(task, return_exceptions=True)
                raise

    async def set_interaction_violated(self, engagement_id: str) -> None:
        """W2/Sol B9 (Task 7): flag a mutating tool-use taken while
        ``awaiting_operator`` — ``_finalize_engagement`` reads
        ``rec.origin.get("interaction_violated")`` to append a violation
        line to the completion summary. Unknown engagement is a no-op
        (matches the other mutators' tolerance for stale callers).

        B3 (Sol diff r2): persist STRICTLY and roll back on failure, mirroring
        ``advance_interaction_state``. The driver seam
        (``claude_code_driver._on_stream_event``) only marks
        ``_violation_flagged`` after a SUCCESSFUL return, so a swallowed write
        failure would permanently drop the completion warning after a restart;
        raising lets the seam retry on the next mutating-tool frame.
        """
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            had_flag = "interaction_violated" in rec.origin
            prev = rec.origin.get("interaction_violated")
            rec.origin["interaction_violated"] = True
            try:
                await self._write_tombstone_locked(strict=True)
            except Exception:
                if had_flag:
                    rec.origin["interaction_violated"] = prev
                else:
                    rec.origin.pop("interaction_violated", None)
                raise

    # -- v0.79.0 (§4) question numbering + open-question ledger --------------

    async def allocate_question_number(self, engagement_id: str) -> int | None:
        """Atomically allocate the next durable ``Q<n>`` for an engagement.

        Bumps ``next_question_number`` under the lock and persists (same
        transactional shield-and-await pattern as ``advance_interaction_state``
        so a cancelled caller never tears the counter from disk). Returns the
        allocated number, or ``None`` for an unknown engagement."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return None
            allocated = rec.next_question_number
            prev = rec.next_question_number
            rec.next_question_number = allocated + 1

            async def _mutate_and_persist() -> int:
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    rec.next_question_number = prev
                    raise
                return allocated

            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)
                raise

    async def add_open_question(
        self, engagement_id: str, number: int, tg_message_id: int | None,
        text: str | None = None, kind: str = "button",
    ) -> None:
        """Record a still-open question ``{n, tg_message_id, text, kind}``
        (persisted). ``text`` is the canonical displayed question so boot
        reconciliation can re-render the settle copy over it (memory-only broker
        state does not survive a restart). ``kind`` is ``"button"`` (broker tap)
        or ``"anchor"`` (free-text — settled by the next operator message).
        Idempotent on ``number``.

        v0.79.0 (§4, Sol F6): STRICT persistence — a tombstone-write failure
        rolls ``open_questions`` back (full-field) and RE-RAISES rather than
        silently leaving a keyboard that the ledger/summary/boot-reconciler
        cannot see. The ask handler settles the keyboard fail-closed on the
        raise. Uses the shield-and-await transactional pattern so cancellation
        during ``to_thread`` cannot split memory from disk."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            prev = rec.open_questions
            entries = [q for q in rec.open_questions if q.get("n") != number]
            entry = {"n": number, "tg_message_id": tg_message_id, "kind": kind}
            if text is not None:
                entry["text"] = text
            entries.append(entry)
            rec.open_questions = tuple(entries)

            async def _mutate_and_persist() -> None:
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    rec.open_questions = prev
                    raise

            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)
                raise

    def oldest_open_anchor(self, engagement_id: str) -> dict | None:
        """The oldest still-open FREE-TEXT anchor (``kind == "anchor"``), or
        ``None``. The next operator message settles it (§4)."""
        rec = self._records.get(engagement_id)
        if rec is None:
            return None
        anchors = [q for q in rec.open_questions if q.get("kind") == "anchor"]
        if not anchors:
            return None
        return min(anchors, key=lambda q: q.get("n", 0))

    async def close_open_question(self, engagement_id: str, number: int) -> None:
        """Remove a settled question from the open-question ledger (persisted).
        ``next_question_number`` is NEVER rewound. Unknown engagement/number is
        a no-op."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            remaining = tuple(
                q for q in rec.open_questions if q.get("n") != number
            )
            if len(remaining) == len(rec.open_questions):
                return
            rec.open_questions = remaining
            await self._write_tombstone_locked()

    def open_question_numbers(self, engagement_id: str) -> list[int]:
        """Accessor for summary consumers (T4): the sorted list of still-open
        question numbers (``Open questions: Q4, Q6``)."""
        rec = self._records.get(engagement_id)
        if rec is None:
            return []
        return sorted(q["n"] for q in rec.open_questions if "n" in q)

    # -- v0.79.0 (§5) live-summary state ------------------------------------

    async def set_summary_message_id(
        self, engagement_id: str, message_id: int | None,
    ) -> None:
        """Persist the pinned summary Telegram message id (posted at boot).
        No-op for an unknown engagement.

        v0.79.0 (§5, Sol F6): STRICT persistence — a tombstone-write failure
        rolls ``summary_message_id`` back and RE-RAISES rather than leaving a
        posted-but-unpersisted summary that a restart cannot resume; the boot
        summary post ABORTS the launch on the raise (§5 post-failure-aborts).
        Shield-and-await so cancellation during ``to_thread`` cannot split
        memory from disk."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            prev = rec.summary_message_id
            rec.summary_message_id = message_id

            async def _mutate_and_persist() -> None:
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    rec.summary_message_id = prev
                    raise

            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)
                raise

    async def allocate_summary_revision(self, engagement_id: str) -> int | None:
        """Atomically allocate the next monotonic summary REVISION (§5).

        Every lifecycle status transition acquires its revision here, so the
        three status sources (driver turn lifecycle, ``interaction_state``, ask
        registry) are totally ordered and collision-free. Uses the same
        transactional shield-and-await pattern as ``allocate_question_number``
        (a cancelled caller never tears the counter from disk). Returns the
        allocated revision, or ``None`` for an unknown engagement."""
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return None
            allocated = rec.summary_revision
            prev = rec.summary_revision
            rec.summary_revision = allocated + 1

            async def _mutate_and_persist() -> int:
                try:
                    await self._write_tombstone_locked(strict=True)
                except Exception:
                    rec.summary_revision = prev
                    raise
                return allocated

            task = asyncio.ensure_future(_mutate_and_persist())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)
                raise

    async def sweep_idle_and_suspend(
        self, *, driver: Any, now_override: float | None = None,
    ) -> None:
        """Daily scan: fire idle_detected + tear down clients past suspend threshold.

        ``driver`` is the ``DriverProtocol`` instance for in_casa — used to
        check is_alive, read session_id, and close the client. For tests,
        ``now_override`` short-circuits ``time.time()``.
        """
        import time
        from bus import BusMessage, MessageType

        now = now_override if now_override is not None else time.time()

        for rec in list(self.active_and_idle()):
            idle_s = now - rec.last_user_turn_ts

            # 1) Session suspension (in_casa only)
            if (rec.driver == "in_casa" and rec.status == "active"
                    and idle_s > _SESSION_SUSPEND_IDLE_S
                    and driver.is_alive(rec)):
                session_id = driver.get_session_id(rec)
                try:
                    await driver.cancel(rec)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "sweep: driver.cancel(%s) failed: %s", rec.id[:8], exc,
                    )
                if session_id is not None:
                    await self.persist_session_id(rec.id, session_id)
                await self.mark_idle(rec.id)

            # 2) Idle reminder
            threshold_s = (
                _IDLE_REMINDER_DAYS_SPECIALIST * 86400
                if rec.kind == "specialist"
                else _IDLE_REMINDER_DAYS_EXECUTOR * 86400
            )
            if idle_s > threshold_s and (
                rec.last_idle_reminder_ts == 0
                or now - rec.last_idle_reminder_ts > _IDLE_REMINDER_REFIRE_DAYS * 86400
            ):
                if self._bus is not None:
                    try:
                        await self._bus.notify(BusMessage(
                            type=MessageType.NOTIFICATION,
                            source=rec.role_or_type,
                            target="observer",
                            content={
                                "event": "idle_detected",
                                "engagement_id": rec.id,
                                "last_user_turn_ts": rec.last_user_turn_ts,
                                "idle_days": int(idle_s // 86400),
                            },
                            context={"engagement_id": rec.id},
                        ))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("sweep: idle notify failed: %s", exc)
                await self.update_last_idle_reminder(rec.id, now)
