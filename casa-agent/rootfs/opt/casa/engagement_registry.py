"""Engagement primitive — Tier 2 Specialist interactive mode + (Plan 3+) Tier 3 Executors.

Symmetric with :mod:`specialist_registry`. Owns:
- EngagementRecord (one in-flight engagement)
- EngagementRegistry (in-memory dict + ``/data/engagements.json`` tombstone)
- Idle sweep (fires ``idle_detected`` bus events + session-suspends live clients)
- Orphan recovery (startup: load tombstone; records remain dormant until
  next user turn in their topic)

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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sweep constants
# ---------------------------------------------------------------------------

_IDLE_REMINDER_DAYS_SPECIALIST = 3
_IDLE_REMINDER_DAYS_EXECUTOR = 7          # default; per-type override lands Plan 3
_IDLE_REMINDER_REFIRE_DAYS = 7
_SESSION_SUSPEND_IDLE_S = 86400
_IDLE_SWEEP_CRON = "0 8 * * *"            # daily 08:00 user TZ


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
      idle    ──next user turn──▶ active
      active  ──emit_completion / /complete──▶ completed
      active  ──/cancel / cancel_engagement──▶ cancelled
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
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping malformed engagement row: %s", exc)
                continue
            self._records[rec.id] = rec
            if rec.topic_id is not None:
                self._topic_index[rec.topic_id] = rec.id

    def active_and_idle(self) -> list[EngagementRecord]:
        return [r for r in self._records.values() if r.status in ("active", "idle")]

    def get(self, engagement_id: str) -> EngagementRecord | None:
        return self._records.get(engagement_id)

    def by_topic_id(self, topic_id: int) -> EngagementRecord | None:
        rec_id = self._topic_index.get(topic_id)
        return self._records.get(rec_id) if rec_id else None

    # -- Persist helper ---------------------------------------------------

    async def _write_tombstone_locked(self) -> None:
        """Caller MUST hold self._lock."""
        snapshot = []
        for rec in self._records.values():
            if rec.status in ("completed", "cancelled", "error"):
                # Finished records are dropped from disk — Ellen's meta-scope
                # memory carries the durable record (spec §4.6).
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
            })
        try:
            await asyncio.to_thread(self._write_tombstone, snapshot)
        except Exception as exc:
            logger.warning("Failed to persist engagement tombstone: %s", exc)

    def _write_tombstone(self, snapshot: list[dict[str, Any]]) -> None:
        with open(self._tombstone_path, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2)

    # -- Mutators ---------------------------------------------------------

    async def create(
        self,
        kind: str,
        role_or_type: str,
        driver: str,
        task: str,
        origin: dict[str, Any],
        topic_id: int | None,
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

    async def mark_completed(self, engagement_id: str, completed_at: float) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.status = "completed"
            rec.completed_at = completed_at
            await self._write_tombstone_locked()

    async def mark_cancelled(self, engagement_id: str) -> None:
        async with self._lock:
            rec = self._records.get(engagement_id)
            if rec is None:
                return
            rec.status = "cancelled"
            rec.completed_at = time.time()
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
            await self._write_tombstone_locked()

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
