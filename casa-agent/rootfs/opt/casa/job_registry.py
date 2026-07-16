"""Durable specialist voice-job state machine.

The registry is the single owner of execution and voice-delivery lifecycle
state.  Runtime-only task, cooperative-cancellation, and concurrency-permit
objects are deliberately kept out of the JSON snapshot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping

from atomic_io import atomic_write_json


logger = logging.getLogger(__name__)


class ExecutionState(StrEnum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ORPHANED = "ORPHANED"


class DeliveryState(StrEnum):
    NONE = "NONE"
    READY = "READY"
    CLAIMED = "CLAIMED"
    AUTHORIZED = "AUTHORIZED"
    PLAYING = "PLAYING"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class JobFailure:
    """Stable failure envelope safe to persist and deliver after restart."""

    kind: str
    message: str


@dataclass(frozen=True)
class VoiceJob:
    """One durable delegated job and its delivery compare-and-set state."""

    id: str
    parent_job_id: str | None
    creating_role: str
    specialist_role: str
    specialist_display_name: str
    creator_peer: str
    creator_user_id: str | None
    scope_id: str
    origin_route_id: str | None
    origin_device_id: str | None
    task: str
    context: str
    created_at: float
    started_at: float | None
    terminal_at: float | None
    expires_at: float | None
    execution_state: ExecutionState
    delivery_state: DeliveryState
    result: str | None
    failure: JobFailure | None
    awaiting_input: bool
    continuable_until: float | None
    delivery_sequence: int
    delivery_attempt_id: str | None
    lease_until: float | None
    cancel_pending: bool
    orphan_notification_pending: bool = False


@dataclass(frozen=True)
class CancelResult:
    status: str


class JobRegistryError(RuntimeError):
    """Base class for durable job registry failures."""


class JobTransitionError(JobRegistryError):
    """A compare-and-set transition did not match the persisted state."""


class JobAuthorizationError(JobRegistryError):
    """The cancellation actor does not own the job's creation scope."""


class JobRegistry:
    """Crash-safe registry for delegated execution and voice delivery.

    A mutation is published to memory only after the complete candidate
    snapshot has been atomically replaced on disk while ``_lock`` is held.
    """

    LEASE_SECONDS = 15.0
    RESULT_TTL_SECONDS = 24 * 60 * 60.0
    CANCEL_GRACE_SECONDS = 2.0

    _TERMINAL_EXECUTION = frozenset({
        ExecutionState.SUCCEEDED,
        ExecutionState.FAILED,
        ExecutionState.CANCELLED,
        ExecutionState.ORPHANED,
    })
    _LEASED_DELIVERY = frozenset({
        DeliveryState.CLAIMED,
        DeliveryState.AUTHORIZED,
        DeliveryState.PLAYING,
    })

    def __init__(
        self,
        path: str | os.PathLike[str],
        legacy_tombstone_path: str | os.PathLike[str],
        *,
        clock: Callable[[], float] = time.time,
        reconciliation_retry_interval: float = 5.0,
    ) -> None:
        retry_interval = float(reconciliation_retry_interval)
        if not math.isfinite(retry_interval) or retry_interval <= 0:
            raise ValueError("reconciliation_retry_interval must be positive")
        self._path = os.fspath(path)
        self._legacy_tombstone_path = os.fspath(legacy_tombstone_path)
        self._clock = clock
        self._jobs: dict[str, VoiceJob] = {}
        self._delivery_sequence = 0
        self._lock = asyncio.Lock()
        self._loaded = False

        # Process-local ownership.  None of these values is JSON-serializable
        # or meaningful after a restart.
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._permits: dict[str, Any] = {}
        self._cancel_timers: dict[str, asyncio.Task] = {}
        self._reconciliation_retry_interval = retry_interval
        self._reconciliation_tasks: dict[str, asyncio.Task] = {}
        self._reconciliation_waiters: dict[
            str, set[asyncio.Future[None]]
        ] = {}
        self._terminal_waiters: dict[
            str, set[asyncio.Future[VoiceJob]]
        ] = {}
        self._runtime_release_waiters: dict[
            str, set[asyncio.Future[None]]
        ] = {}

        # IDs migrated from the old tombstone during this process's first
        # load.  recover_after_restart consumes this list for Telegram's
        # compatibility notification path; it is never a second lifecycle.
        self._migrated_on_load: list[str] = []

    @property
    def path(self) -> str:
        return self._path

    async def load(self) -> None:
        """Load the durable snapshot, migrating legacy tombstones once."""
        async with self._lock:
            if self._loaded:
                return

            snapshot_exists = os.path.exists(self._path)
            if snapshot_exists:
                raw = await asyncio.to_thread(self._read_json, self._path)
                jobs = self._decode_snapshot(raw)
            else:
                jobs = {}

            migrated: dict[str, VoiceJob] = {}
            migrated_ids: list[str] = []
            consume_legacy = False
            legacy_exists = os.path.exists(self._legacy_tombstone_path)
            if legacy_exists:
                migrated, migrated_ids, consume_legacy = await asyncio.to_thread(
                    self._read_legacy_jobs,
                    max((job.delivery_sequence for job in jobs.values()), default=0),
                    jobs,
                )
                jobs.update(migrated)

            write_snapshot = not snapshot_exists or bool(migrated)

            def publish_load() -> None:
                self._jobs = jobs
                self._delivery_sequence = max(
                    (job.delivery_sequence for job in jobs.values()), default=0,
                )
                # Telegram recovery is driven exclusively by the durable
                # pending bit. This process-local bridge remains only for
                # migrated voice rows, whose READY delivery is authoritative.
                self._migrated_on_load = [
                    job_id for job_id in migrated_ids
                    if (job_id in jobs
                        and not jobs[job_id].orphan_notification_pending)
                ]
                self._loaded = True

            if write_snapshot or consume_legacy:
                async def commit_load() -> None:
                    if write_snapshot:
                        # Persist converted rows after any existing jobs and
                        # before touching the legacy file. A failed write
                        # therefore cannot lose the only recovery copy.
                        await self._write_snapshot_locked(jobs)
                    if consume_legacy:
                        await asyncio.to_thread(
                            atomic_write_json,
                            self._legacy_tombstone_path,
                            [],
                            indent=2,
                        )
                    publish_load()

                await self._finish_atomic_commit(commit_load())
            else:
                publish_load()

    def get(self, job_id: str) -> VoiceJob | None:
        return self._jobs.get(job_id)

    def all(self) -> list[VoiceJob]:
        """Return a stable delivery-order snapshot."""
        return sorted(
            self._jobs.values(),
            key=lambda job: (job.delivery_sequence, job.created_at, job.id),
        )

    async def create(self, job: VoiceJob) -> VoiceJob:
        async with self._lock:
            self._require_loaded()
            if not job.id:
                raise ValueError("job id must not be empty")
            if job.id in self._jobs:
                raise JobTransitionError(f"job {job.id!r} already exists")
            candidate = dict(self._jobs)
            candidate[job.id] = job
            await self._commit_snapshot_locked(candidate)
            return job

    async def create_continuation(
        self,
        parent_job_id: str,
        child: VoiceJob,
        *,
        actor: Any,
    ) -> VoiceJob:
        """Consume one live clarification and create its child atomically."""
        async with self._lock:
            self._require_loaded()
            parent = self._require_job(parent_job_id)
            self._authorize_actor(parent, actor)
            self._authorize_actor(child, actor)
            if not child.id:
                raise ValueError("job id must not be empty")
            if child.id in self._jobs:
                raise JobTransitionError(f"job {child.id!r} already exists")
            if child.parent_job_id != parent_job_id:
                raise ValueError("continuation child has the wrong parent")
            if child.specialist_role != parent.specialist_role:
                raise ValueError("continuation child has the wrong specialist")
            if (child.execution_state is not ExecutionState.ACCEPTED
                    or child.delivery_state is not DeliveryState.NONE
                    or child.result is not None
                    or child.failure is not None
                    or child.awaiting_input
                    or child.continuable_until is not None):
                raise ValueError("continuation child must be newly accepted")

            now = self._now()
            continuable = (
                parent.execution_state is ExecutionState.SUCCEEDED
                and parent.awaiting_input
                and parent.continuable_until is not None
                and parent.continuable_until > now
                and (parent.expires_at is None or parent.expires_at > now)
                and parent.delivery_state not in {
                    DeliveryState.CANCELLED,
                    DeliveryState.EXPIRED,
                }
                and not parent.cancel_pending
                and parent.result is not None
            )
            if not continuable:
                raise self._transition_error(
                    parent,
                    "create_continuation",
                    expected="live awaiting-input parent",
                )

            consumed_parent = replace(
                parent,
                awaiting_input=False,
            )
            candidate = dict(self._jobs)
            candidate[parent_job_id] = consumed_parent
            candidate[child.id] = child
            await self._commit_snapshot_locked(candidate)
            return child

    async def compensate_unbound_continuation(
        self,
        parent_job_id: str,
        child_job_id: str,
        *,
        actor: Any,
    ) -> bool:
        """Remove an unbound child and restore its still-fresh parent."""
        async with self._lock:
            self._require_loaded()
            parent = self._require_job(parent_job_id)
            self._authorize_actor(parent, actor)
            child = self._jobs.get(child_job_id)
            if child is None:
                return False
            self._authorize_actor(child, actor)
            if child.parent_job_id != parent_job_id:
                raise JobTransitionError(
                    f"job {child_job_id!r} is not a child of {parent_job_id!r}"
                )
            if (child.execution_state is not ExecutionState.ACCEPTED
                    or child_job_id in self._tasks):
                return False

            now = self._now()
            restore_parent = (
                parent.execution_state is ExecutionState.SUCCEEDED
                and not parent.awaiting_input
                and parent.continuable_until is not None
                and parent.continuable_until > now
                and (parent.expires_at is None or parent.expires_at > now)
                and parent.delivery_state not in {
                    DeliveryState.CANCELLED,
                    DeliveryState.EXPIRED,
                }
                and not parent.cancel_pending
                and parent.result is not None
            )
            candidate = dict(self._jobs)
            candidate.pop(child_job_id)
            if restore_parent:
                candidate[parent_job_id] = replace(parent, awaiting_input=True)
            await self._commit_snapshot_locked(candidate)
            return restore_parent

    def owns_task(self, job_id: str, task: asyncio.Task) -> bool:
        """Return whether runtime ownership was published for exactly task."""
        return self._tasks.get(job_id) is task

    async def wait_for_terminal(self, job_id: str) -> VoiceJob:
        """Wait for a concrete durable terminal transition for one job."""
        async with self._lock:
            current = self._require_job(job_id)
            if current.execution_state in self._TERMINAL_EXECUTION:
                return current
            future = asyncio.get_running_loop().create_future()
            self._terminal_waiters.setdefault(job_id, set()).add(future)
        try:
            return await future
        finally:
            waiters = self._terminal_waiters.get(job_id)
            if waiters is not None:
                waiters.discard(future)
                if not waiters:
                    self._terminal_waiters.pop(job_id, None)

    async def wait_for_runtime_release(self, job_id: str) -> None:
        """Wait until the bound task has released its runtime ownership."""
        if job_id not in self._tasks:
            return
        future = asyncio.get_running_loop().create_future()
        self._runtime_release_waiters.setdefault(job_id, set()).add(future)
        if job_id not in self._tasks and not future.done():
            future.set_result(None)
        try:
            await future
        finally:
            waiters = self._runtime_release_waiters.get(job_id)
            if waiters is not None:
                waiters.discard(future)
                if not waiters:
                    self._runtime_release_waiters.pop(job_id, None)

    @property
    def reconciliation_count(self) -> int:
        return len(self._reconciliation_tasks)

    async def wait_for_reconciliation(self, job_id: str) -> None:
        """Wait until registry-owned terminal reconciliation is drained."""
        task = self._reconciliation_tasks.get(job_id)
        if task is None or task.done():
            return
        future = asyncio.get_running_loop().create_future()
        self._reconciliation_waiters.setdefault(job_id, set()).add(future)
        current = self._reconciliation_tasks.get(job_id)
        if (current is not task or task.done()) and not future.done():
            future.set_result(None)
        try:
            await future
        finally:
            waiters = self._reconciliation_waiters.get(job_id)
            if waiters is not None:
                waiters.discard(future)
                if not waiters:
                    self._reconciliation_waiters.pop(job_id, None)

    def schedule_failure_reconciliation(self, job_id: str) -> None:
        """Strongly own a metadata-only retry for a still-live failed write."""
        existing = self._reconciliation_tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._reconcile_failure(job_id))
        self._reconciliation_tasks[job_id] = task
        task.add_done_callback(
            lambda done, jid=job_id: self._reconciliation_done(jid, done),
        )

    async def _reconcile_failure(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(self._reconciliation_retry_interval)
            try:
                current = await self.fail_compat(
                    job_id,
                    JobFailure(
                        "persistence_failed",
                        "Specialist result could not be saved.",
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never render persistence content
                logger.warning(
                    "job %s terminal reconciliation retry failed",
                    job_id[:8],
                )
                continue
            if (current is None
                    or current.execution_state in self._TERMINAL_EXECUTION):
                return

    def _reconciliation_done(self, job_id: str, task: asyncio.Task) -> None:
        if self._reconciliation_tasks.get(job_id) is task:
            self._reconciliation_tasks.pop(job_id, None)
        for waiter in self._reconciliation_waiters.pop(job_id, set()):
            if not waiter.done():
                waiter.set_result(None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("job %s terminal reconciliation stopped", job_id[:8])

    async def bind_task(
        self,
        job_id: str,
        task: asyncio.Task,
        permit: Any = None,
        cancel_event: asyncio.Event | None = None,
    ) -> asyncio.Event:
        """Transition ACCEPTED→RUNNING and bind runtime task ownership.

        The installed done callback is the sole release authority for the
        bound permit.  Terminal record transitions intentionally never touch
        it because cancellation can be persisted before the task has actually
        finished unwinding.
        """
        async with self._lock:
            self._require_loaded()
            current = self._require_job(job_id)
            if current.execution_state is not ExecutionState.ACCEPTED:
                raise self._transition_error(
                    current, "bind_task", expected="execution=ACCEPTED",
                )
            if job_id in self._tasks:
                raise JobTransitionError(f"job {job_id!r} already has a task")
            updated = replace(
                current,
                execution_state=ExecutionState.RUNNING,
                started_at=(current.started_at
                            if current.started_at is not None else self._now()),
            )
            candidate = self._with_job(updated)

            event = cancel_event or asyncio.Event()

            def publish_runtime_ownership() -> None:
                self._tasks[job_id] = task
                self._cancel_events[job_id] = event
                if permit is not None:
                    self._permits[job_id] = permit
                task.add_done_callback(
                    lambda done, jid=job_id: self._task_done(jid, done),
                )

            await self._commit_snapshot_locked(
                candidate, after_publish=publish_runtime_ownership,
            )
            return event

    async def finish(self, job_id: str, result: str) -> VoiceJob:
        """Persist a successful terminal execution and queue voice delivery."""
        async with self._lock:
            current = self._require_job(job_id)
            self._require_live_execution(current, "finish")
            return await self._finish_current_locked(current, result)

    async def finish_voice_result(
        self,
        job_id: str,
        result: str,
        *,
        awaiting_input: bool,
        delivery_ttl_s: int,
    ) -> VoiceJob:
        """Atomically persist a validated structured voice-job result."""
        if not isinstance(awaiting_input, bool):
            raise ValueError("awaiting_input must be a boolean")
        if (isinstance(delivery_ttl_s, bool)
                or not isinstance(delivery_ttl_s, int)
                or not 30 <= delivery_ttl_s <= 3600):
            raise ValueError("delivery_ttl_s must be an integer from 30 to 3600")

        async with self._lock:
            current = self._require_job(job_id)
            self._require_live_execution(current, "finish_voice_result")
            return await self._finish_voice_result_current_locked(
                current,
                result,
                awaiting_input=awaiting_input,
                delivery_ttl_s=delivery_ttl_s,
            )

    async def fail(
        self,
        job_id: str,
        failure: JobFailure | BaseException,
    ) -> VoiceJob:
        """Persist a failed/cancelled terminal execution."""
        async with self._lock:
            current = self._require_job(job_id)
            self._require_live_execution(current, "fail")
            return await self._fail_current_locked(current, failure)

    async def finish_compat(
        self, job_id: str, result: str = "",
    ) -> VoiceJob | None:
        """Idempotently finish a live job for legacy delegation callbacks."""
        async with self._lock:
            self._require_loaded()
            current = self._jobs.get(job_id)
            if (current is None
                    or current.execution_state not in {
                        ExecutionState.ACCEPTED, ExecutionState.RUNNING,
                    }):
                return current
            return await self._finish_current_locked(current, result)

    async def fail_compat(
        self, job_id: str, failure: JobFailure | BaseException,
    ) -> VoiceJob | None:
        """Idempotently fail a live job for legacy delegation callbacks."""
        async with self._lock:
            self._require_loaded()
            current = self._jobs.get(job_id)
            if (current is None
                    or current.execution_state not in {
                        ExecutionState.ACCEPTED, ExecutionState.RUNNING,
                    }):
                return current
            return await self._fail_current_locked(current, failure)

    async def request_cancel(self, job_id: str, *, actor: Any) -> CancelResult:
        """Authorize creator cancellation without racing playback start."""
        async with self._lock:
            current = self._require_job(job_id)
            self._authorize_actor(current, actor)

            if current.delivery_state in {
                DeliveryState.PLAYING, DeliveryState.DELIVERED,
            }:
                return CancelResult("too_late")
            if current.delivery_state in {
                DeliveryState.CANCELLED, DeliveryState.EXPIRED,
            } or current.execution_state is ExecutionState.CANCELLED:
                return CancelResult("cancelled")

            if current.delivery_state is DeliveryState.AUTHORIZED:
                updated = replace(current, cancel_pending=True)
                status = "stopping"
            elif current.delivery_state in {
                DeliveryState.READY, DeliveryState.CLAIMED,
            }:
                updated = replace(
                    current,
                    delivery_state=DeliveryState.CANCELLED,
                    delivery_attempt_id=None,
                    lease_until=None,
                    cancel_pending=False,
                )
                status = "cancelled"
            elif current.execution_state in {
                ExecutionState.ACCEPTED, ExecutionState.RUNNING,
            }:
                updated = replace(current, cancel_pending=True)
                status = "stopping"
            else:
                return CancelResult("too_late")

            event = self._cancel_events.get(job_id)
            task = self._tasks.get(job_id)

            def publish_cancel_signal() -> None:
                if event is not None:
                    event.set()
                if task is not None and not task.done():
                    self._arm_force_cancel(job_id, task)

            await self._persist_job_locked(
                updated, after_publish=publish_cancel_signal,
            )
            return CancelResult(status)

    async def cancel(self, job_id: str) -> VoiceJob | None:
        """Compatibility terminal transition used by legacy delegation code."""
        async with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                return None
            if current.execution_state in self._TERMINAL_EXECUTION:
                return current
            now = self._now()
            updated = replace(
                current,
                execution_state=ExecutionState.CANCELLED,
                terminal_at=now,
                expires_at=now + self.RESULT_TTL_SECONDS,
                failure=JobFailure("cancelled", "Delegation cancelled"),
                delivery_state=(
                    DeliveryState.CANCELLED
                    if current.delivery_state is not DeliveryState.NONE
                    else DeliveryState.NONE
                ),
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
            return await self._persist_job_locked(updated)

    async def claim(self, job_id: str, delivery_attempt_id: str) -> VoiceJob:
        if not delivery_attempt_id:
            raise ValueError("delivery_attempt_id must not be empty")
        async with self._lock:
            current = self._require_delivery_cas(
                job_id, "claim", DeliveryState.READY, attempt_id=None,
            )
            updated = replace(
                current,
                delivery_state=DeliveryState.CLAIMED,
                delivery_attempt_id=delivery_attempt_id,
                lease_until=self._now() + self.LEASE_SECONDS,
            )
            return await self._persist_job_locked(updated)

    async def renew(self, job_id: str, delivery_attempt_id: str) -> VoiceJob:
        async with self._lock:
            current = self._require_job(job_id)
            if (current.delivery_state not in self._LEASED_DELIVERY
                    or current.delivery_attempt_id != delivery_attempt_id):
                raise self._transition_error(
                    current, "renew",
                    expected="matching attempt in CLAIMED/AUTHORIZED/PLAYING",
                )
            updated = replace(
                current, lease_until=self._now() + self.LEASE_SECONDS,
            )
            return await self._persist_job_locked(updated)

    async def authorize(self, job_id: str, delivery_attempt_id: str) -> VoiceJob:
        async with self._lock:
            current = self._require_delivery_cas(
                job_id, "authorize", DeliveryState.CLAIMED,
                attempt_id=delivery_attempt_id,
            )
            updated = replace(current, delivery_state=DeliveryState.AUTHORIZED)
            return await self._persist_job_locked(updated)

    async def mark_playing(
        self, job_id: str, delivery_attempt_id: str,
    ) -> VoiceJob:
        async with self._lock:
            current = self._require_delivery_cas(
                job_id, "mark_playing", DeliveryState.AUTHORIZED,
                attempt_id=delivery_attempt_id,
            )
            if current.cancel_pending:
                raise self._transition_error(
                    current,
                    "mark_playing",
                    expected="AUTHORIZED without cancel_pending",
                )
            updated = replace(current, delivery_state=DeliveryState.PLAYING)
            return await self._persist_job_locked(updated)

    async def mark_delivered(
        self, job_id: str, delivery_attempt_id: str,
    ) -> VoiceJob:
        async with self._lock:
            current = self._require_delivery_cas(
                job_id, "mark_delivered", DeliveryState.PLAYING,
                attempt_id=delivery_attempt_id,
            )
            updated = replace(
                current,
                delivery_state=DeliveryState.DELIVERED,
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
            return await self._persist_job_locked(updated)

    async def nack(
        self,
        job_id: str,
        delivery_attempt_id: str,
        reason: str,
    ) -> VoiceJob:
        async with self._lock:
            current = self._require_job(job_id)
            if (current.delivery_state not in {
                    DeliveryState.CLAIMED, DeliveryState.AUTHORIZED,
                } or current.delivery_attempt_id != delivery_attempt_id):
                raise self._transition_error(
                    current, "nack",
                    expected="matching attempt in CLAIMED/AUTHORIZED",
                )
            cancelled = (
                current.delivery_state is DeliveryState.AUTHORIZED
                and current.cancel_pending
                and reason == "preempted_before_playback"
            )
            updated = replace(
                current,
                delivery_state=(
                    DeliveryState.CANCELLED if cancelled else DeliveryState.READY
                ),
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
            return await self._persist_job_locked(updated)

    async def expire_due(self) -> list[VoiceJob]:
        """Apply terminal result/delivery TTL without deleting audit records."""
        async with self._lock:
            now = self._now()
            changed: list[VoiceJob] = []
            candidate = dict(self._jobs)
            for job_id, current in self._jobs.items():
                if current.expires_at is None or current.expires_at > now:
                    continue
                if current.delivery_state in {
                    DeliveryState.DELIVERED,
                    DeliveryState.CANCELLED,
                    DeliveryState.EXPIRED,
                }:
                    continue
                updated = replace(
                    current,
                    delivery_state=DeliveryState.EXPIRED,
                    delivery_attempt_id=None,
                    lease_until=None,
                    cancel_pending=False,
                )
                candidate[job_id] = updated
                changed.append(updated)
            if changed:
                await self._commit_snapshot_locked(candidate)
            return changed

    async def expire_leases(self) -> list[VoiceJob]:
        """Recover lapsed delivery attempts independently from result TTL."""
        async with self._lock:
            now = self._now()
            changed: list[VoiceJob] = []
            candidate = dict(self._jobs)
            for job_id, current in self._jobs.items():
                if (current.delivery_state not in self._LEASED_DELIVERY
                        or current.lease_until is None
                        or current.lease_until > now):
                    continue
                cancelled = (
                    current.delivery_state is DeliveryState.AUTHORIZED
                    and current.cancel_pending
                )
                updated = replace(
                    current,
                    delivery_state=(
                        DeliveryState.CANCELLED
                        if cancelled else DeliveryState.READY
                    ),
                    delivery_attempt_id=None,
                    lease_until=None,
                    cancel_pending=False,
                )
                candidate[job_id] = updated
                changed.append(updated)
            if changed:
                await self._commit_snapshot_locked(candidate)
            return changed

    async def recover_after_restart(self) -> list[VoiceJob]:
        """Recover execution and retain delivery attempts for one full lease."""
        async with self._lock:
            self._require_loaded()
            now = self._now()
            recovered_ids = [
                job.id for job in self.all()
                if job.orphan_notification_pending
            ]
            recovered_ids.extend(self._migrated_on_load)
            candidate = dict(self._jobs)
            changed = False
            next_sequence = self._delivery_sequence

            for job_id, current in self._jobs.items():
                updated = current
                if current.execution_state in {
                    ExecutionState.ACCEPTED,
                    ExecutionState.RUNNING,
                }:
                    if current.origin_route_id and current.origin_device_id:
                        next_sequence += 1
                        delivery = DeliveryState.READY
                        sequence = next_sequence
                    else:
                        delivery = DeliveryState.NONE
                        sequence = current.delivery_sequence
                    updated = replace(
                        current,
                        execution_state=ExecutionState.ORPHANED,
                        terminal_at=now,
                        expires_at=now + self.RESULT_TTL_SECONDS,
                        failure=JobFailure(
                            "restart_orphan", "Lost on restart",
                        ),
                        delivery_state=delivery,
                        delivery_sequence=sequence,
                        delivery_attempt_id=None,
                        lease_until=None,
                        cancel_pending=False,
                        orphan_notification_pending=(
                            current.creator_peer == "telegram"
                        ),
                    )
                    recovered_ids.append(job_id)
                elif current.delivery_state in self._LEASED_DELIVERY:
                    # A restarted coordinator must not immediately steal an
                    # attempt that may still be speaking through the device.
                    updated = replace(
                        current, lease_until=now + self.LEASE_SECONDS,
                    )
                if updated != current:
                    candidate[job_id] = updated
                    changed = True

            if changed:
                await self._commit_snapshot_locked(
                    candidate, after_publish=self._migrated_on_load.clear,
                )
            else:
                self._migrated_on_load.clear()

            seen: set[str] = set()
            return [
                self._jobs[job_id]
                for job_id in recovered_ids
                if job_id in self._jobs and not (job_id in seen or seen.add(job_id))
            ]

    async def ack_orphan_notification(self, job_id: str) -> VoiceJob:
        """Durably acknowledge a restart-orphan Telegram notification."""
        async with self._lock:
            current = self._require_job(job_id)
            if not current.orphan_notification_pending:
                return current
            return await self._persist_job_locked(replace(
                current, orphan_notification_pending=False,
            ))

    async def close(self) -> None:
        """Cancel and drain execution, cancellation, and retry ownership."""
        timers = list(self._cancel_timers.values())
        self._cancel_timers.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)

        tasks = list(self._tasks.values())
        for event in self._cancel_events.values():
            event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        reconciliations = list(self._reconciliation_tasks.values())
        for task in reconciliations:
            if not task.done():
                task.cancel()
        if reconciliations:
            await asyncio.gather(*reconciliations, return_exceptions=True)

    # -- persistence -----------------------------------------------------

    def _read_json(self, path: str) -> Any:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _read_legacy_jobs(
        self,
        starting_sequence: int,
        existing: Mapping[str, VoiceJob],
    ) -> tuple[dict[str, VoiceJob], list[str], bool]:
        try:
            raw = self._read_json(self._legacy_tombstone_path)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(
                "Legacy delegation tombstone corrupt or unreadable (%s): %s",
                self._legacy_tombstone_path, exc,
            )
            return {}, [], True
        if not isinstance(raw, list):
            logger.error(
                "Legacy delegation tombstone %s is not a JSON array",
                self._legacy_tombstone_path,
            )
            return {}, [], True

        jobs: dict[str, VoiceJob] = {}
        migrated_ids: list[str] = []
        sequence = starting_sequence
        now = self._now()
        for row in raw:
            try:
                job_id = str(row["id"])
                agent = str(row["agent"])
                started_at = float(row.get("started_at", 0.0))
                origin = dict(row.get("origin") or {})
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping malformed legacy delegation: %s", exc)
                continue
            if job_id in existing or job_id in jobs:
                prior = existing.get(job_id)
                if (prior is not None
                        and prior.execution_state is ExecutionState.ORPHANED
                        and prior.failure is not None
                        and prior.failure.kind == "restart_orphan"):
                    # Handles a crash after jobs.json replace but before legacy
                    # truncation: do not duplicate the record, but do surface
                    # the recovered failure during this successful boot.
                    if (prior.creator_peer == "telegram"
                            and not prior.orphan_notification_pending):
                        # Backfills a pre-field snapshot caught in that crash
                        # window before consuming its remaining tombstone.
                        jobs[job_id] = replace(
                            prior, orphan_notification_pending=True,
                        )
                    migrated_ids.append(job_id)
                else:
                    logger.warning(
                        "Skipping legacy delegation with duplicate id %s", job_id,
                    )
                continue
            sequence += 1
            route_id = origin.get("cid") or origin.get("route_id")
            device_id = origin.get("device_id") or origin.get("origin_device_id")
            has_voice_route = bool(route_id and device_id)
            job = VoiceJob(
                id=job_id,
                parent_job_id=None,
                creating_role=str(origin.get("role") or "assistant"),
                specialist_role=agent,
                specialist_display_name=agent,
                creator_peer=str(origin.get("channel") or ""),
                creator_user_id=self._optional_str(origin.get("user_id")),
                scope_id=str(origin.get("chat_id") or origin.get("scope_id") or ""),
                origin_route_id=self._optional_str(route_id),
                origin_device_id=self._optional_str(device_id),
                task=str(origin.get("user_text") or ""),
                context="",
                created_at=started_at,
                started_at=started_at,
                terminal_at=now,
                expires_at=now + self.RESULT_TTL_SECONDS,
                execution_state=ExecutionState.ORPHANED,
                delivery_state=(
                    DeliveryState.READY if has_voice_route else DeliveryState.NONE
                ),
                result=None,
                failure=JobFailure("restart_orphan", "Lost on restart"),
                awaiting_input=False,
                continuable_until=None,
                delivery_sequence=sequence,
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
                orphan_notification_pending=(
                    str(origin.get("channel") or "") == "telegram"
                ),
            )
            jobs[job_id] = job
            migrated_ids.append(job_id)
        return jobs, migrated_ids, bool(raw)

    def _decode_snapshot(self, raw: Any) -> dict[str, VoiceJob]:
        if not isinstance(raw, list):
            raise JobRegistryError(f"job snapshot {self._path!r} is not a JSON array")
        jobs: dict[str, VoiceJob] = {}
        for row in raw:
            job = self._decode_job(row)
            if job.id in jobs:
                raise JobRegistryError(f"duplicate job id {job.id!r} in snapshot")
            jobs[job.id] = job
        return jobs

    @staticmethod
    def _decode_job(row: Any) -> VoiceJob:
        if not isinstance(row, dict):
            raise JobRegistryError("job snapshot row is not an object")
        failure_raw = row.get("failure")
        failure = None
        if failure_raw is not None:
            if not isinstance(failure_raw, dict):
                raise JobRegistryError("job failure is not an object")
            failure = JobFailure(
                kind=str(failure_raw["kind"]),
                message=str(failure_raw["message"]),
            )
        try:
            return VoiceJob(
                id=str(row["id"]),
                parent_job_id=JobRegistry._optional_str(row.get("parent_job_id")),
                creating_role=str(row["creating_role"]),
                specialist_role=str(row["specialist_role"]),
                specialist_display_name=str(row["specialist_display_name"]),
                creator_peer=str(row["creator_peer"]),
                creator_user_id=JobRegistry._optional_str(row.get("creator_user_id")),
                scope_id=str(row["scope_id"]),
                origin_route_id=JobRegistry._optional_str(row.get("origin_route_id")),
                origin_device_id=JobRegistry._optional_str(row.get("origin_device_id")),
                task=str(row["task"]),
                context=str(row["context"]),
                created_at=float(row["created_at"]),
                started_at=JobRegistry._optional_float(row.get("started_at")),
                terminal_at=JobRegistry._optional_float(row.get("terminal_at")),
                expires_at=JobRegistry._optional_float(row.get("expires_at")),
                execution_state=ExecutionState(row["execution_state"]),
                delivery_state=DeliveryState(row["delivery_state"]),
                result=(None if row.get("result") is None else str(row["result"])),
                failure=failure,
                awaiting_input=bool(row["awaiting_input"]),
                continuable_until=JobRegistry._optional_float(
                    row.get("continuable_until")),
                delivery_sequence=int(row["delivery_sequence"]),
                delivery_attempt_id=JobRegistry._optional_str(
                    row.get("delivery_attempt_id")),
                lease_until=JobRegistry._optional_float(row.get("lease_until")),
                cancel_pending=bool(row["cancel_pending"]),
                orphan_notification_pending=bool(
                    row.get("orphan_notification_pending", False)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise JobRegistryError(f"invalid job snapshot row: {exc}") from exc

    @staticmethod
    def _encode_job(job: VoiceJob) -> dict[str, Any]:
        return {
            "id": job.id,
            "parent_job_id": job.parent_job_id,
            "creating_role": job.creating_role,
            "specialist_role": job.specialist_role,
            "specialist_display_name": job.specialist_display_name,
            "creator_peer": job.creator_peer,
            "creator_user_id": job.creator_user_id,
            "scope_id": job.scope_id,
            "origin_route_id": job.origin_route_id,
            "origin_device_id": job.origin_device_id,
            "task": job.task,
            "context": job.context,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "terminal_at": job.terminal_at,
            "expires_at": job.expires_at,
            "execution_state": job.execution_state.value,
            "delivery_state": job.delivery_state.value,
            "result": job.result,
            "failure": (
                None if job.failure is None else {
                    "kind": job.failure.kind,
                    "message": job.failure.message,
                }
            ),
            "awaiting_input": job.awaiting_input,
            "continuable_until": job.continuable_until,
            "delivery_sequence": job.delivery_sequence,
            "delivery_attempt_id": job.delivery_attempt_id,
            "lease_until": job.lease_until,
            "cancel_pending": job.cancel_pending,
            "orphan_notification_pending": job.orphan_notification_pending,
        }

    async def _write_snapshot_locked(
        self, jobs: Mapping[str, VoiceJob],
    ) -> None:
        snapshot = [
            self._encode_job(job)
            for job in sorted(
                jobs.values(),
                key=lambda item: (item.delivery_sequence, item.created_at, item.id),
            )
        ]
        await asyncio.to_thread(
            atomic_write_json, self._path, snapshot, indent=2,
        )

    async def _commit_snapshot_locked(
        self,
        jobs: dict[str, VoiceJob],
        *,
        after_publish: Callable[[], None] | None = None,
    ) -> None:
        """Persist and publish one candidate without a cancellation gap."""
        async def commit() -> None:
            await self._write_snapshot_locked(jobs)
            self._jobs = jobs
            self._delivery_sequence = max(
                (job.delivery_sequence for job in jobs.values()), default=0,
            )
            if after_publish is not None:
                after_publish()
            self._signal_terminal_waiters()

        await self._finish_atomic_commit(commit())

    @staticmethod
    async def _finish_atomic_commit(operation: Awaitable[None]) -> None:
        """Defer caller cancellation until a disk/publication commit finishes."""
        task = asyncio.ensure_future(operation)
        cancelled: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc

        # A persistence error wins over a simultaneous cancellation and is
        # always retrieved from the inner task. Publication only happens after
        # the blocking writer returned successfully.
        task.result()
        if cancelled is not None:
            raise cancelled

    # -- transition helpers --------------------------------------------

    async def _finish_current_locked(
        self, current: VoiceJob, result: str,
    ) -> VoiceJob:
        now = self._now()
        if current.cancel_pending:
            updated = replace(
                current,
                execution_state=ExecutionState.CANCELLED,
                terminal_at=now,
                expires_at=now + self.RESULT_TTL_SECONDS,
                failure=JobFailure("cancelled", "Cancelled by creator"),
                delivery_state=(
                    DeliveryState.CANCELLED
                    if current.delivery_state is not DeliveryState.NONE
                    else DeliveryState.NONE
                ),
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
        else:
            delivery, sequence = self._terminal_delivery(current)
            updated = replace(
                current,
                execution_state=ExecutionState.SUCCEEDED,
                terminal_at=now,
                expires_at=now + self.RESULT_TTL_SECONDS,
                result=str(result),
                failure=None,
                delivery_state=delivery,
                delivery_sequence=sequence,
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
        return await self._persist_job_locked(updated)

    async def _finish_voice_result_current_locked(
        self,
        current: VoiceJob,
        result: str,
        *,
        awaiting_input: bool,
        delivery_ttl_s: int,
    ) -> VoiceJob:
        now = self._now()
        if current.cancel_pending:
            updated = replace(
                current,
                execution_state=ExecutionState.CANCELLED,
                terminal_at=now,
                expires_at=now + self.RESULT_TTL_SECONDS,
                failure=JobFailure("cancelled", "Cancelled by creator"),
                awaiting_input=False,
                continuable_until=None,
                delivery_state=(
                    DeliveryState.CANCELLED
                    if current.delivery_state is not DeliveryState.NONE
                    else DeliveryState.NONE
                ),
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
        else:
            expires_at = now + float(delivery_ttl_s)
            delivery, sequence = self._terminal_delivery(current)
            updated = replace(
                current,
                execution_state=ExecutionState.SUCCEEDED,
                terminal_at=now,
                expires_at=expires_at,
                result=str(result),
                failure=None,
                awaiting_input=awaiting_input,
                continuable_until=(expires_at if awaiting_input else None),
                delivery_state=delivery,
                delivery_sequence=sequence,
                delivery_attempt_id=None,
                lease_until=None,
                cancel_pending=False,
            )
        return await self._persist_job_locked(updated)

    async def _fail_current_locked(
        self,
        current: VoiceJob,
        failure: JobFailure | BaseException,
    ) -> VoiceJob:
        now = self._now()
        envelope = self._failure_envelope(failure)
        cancelled = (
            isinstance(failure, asyncio.CancelledError)
            or envelope.kind == "cancelled"
        )
        if cancelled or current.cancel_pending:
            state = ExecutionState.CANCELLED
            delivery = (
                DeliveryState.CANCELLED
                if current.delivery_state is not DeliveryState.NONE
                else DeliveryState.NONE
            )
            sequence = current.delivery_sequence
        else:
            state = ExecutionState.FAILED
            delivery, sequence = self._terminal_delivery(current)
        updated = replace(
            current,
            execution_state=state,
            terminal_at=now,
            expires_at=now + self.RESULT_TTL_SECONDS,
            failure=envelope,
            delivery_state=delivery,
            delivery_sequence=sequence,
            delivery_attempt_id=None,
            lease_until=None,
            cancel_pending=False,
        )
        return await self._persist_job_locked(updated)

    async def _persist_job_locked(
        self,
        updated: VoiceJob,
        *,
        after_publish: Callable[[], None] | None = None,
    ) -> VoiceJob:
        candidate = self._with_job(updated)
        await self._commit_snapshot_locked(
            candidate, after_publish=after_publish,
        )
        return updated

    def _with_job(self, updated: VoiceJob) -> dict[str, VoiceJob]:
        candidate = dict(self._jobs)
        candidate[updated.id] = updated
        return candidate

    def _require_job(self, job_id: str) -> VoiceJob:
        self._require_loaded()
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise JobTransitionError(f"unknown job {job_id!r}") from exc

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise JobRegistryError("JobRegistry.load() must be awaited first")

    def _require_live_execution(self, job: VoiceJob, action: str) -> None:
        if job.execution_state not in {
            ExecutionState.ACCEPTED, ExecutionState.RUNNING,
        }:
            raise self._transition_error(
                job, action, expected="execution=ACCEPTED/RUNNING",
            )

    def _require_delivery_cas(
        self,
        job_id: str,
        action: str,
        state: DeliveryState,
        *,
        attempt_id: str | None,
    ) -> VoiceJob:
        current = self._require_job(job_id)
        if current.delivery_state is not state:
            raise self._transition_error(
                current, action, expected=f"delivery={state.value}",
            )
        if attempt_id is None:
            if current.delivery_attempt_id is not None:
                raise self._transition_error(
                    current, action, expected="no persisted delivery attempt",
                )
        elif current.delivery_attempt_id != attempt_id:
            raise self._transition_error(
                current, action, expected=f"attempt={attempt_id!r}",
            )
        return current

    @staticmethod
    def _transition_error(
        job: VoiceJob, action: str, *, expected: str,
    ) -> JobTransitionError:
        return JobTransitionError(
            f"{action} rejected for {job.id!r}: expected {expected}; "
            f"found execution={job.execution_state.value}, "
            f"delivery={job.delivery_state.value}, "
            f"attempt={job.delivery_attempt_id!r}",
        )

    def _terminal_delivery(
        self, job: VoiceJob,
    ) -> tuple[DeliveryState, int]:
        if not (job.origin_route_id and job.origin_device_id):
            return DeliveryState.NONE, job.delivery_sequence
        return DeliveryState.READY, self._delivery_sequence + 1

    def _task_done(self, job_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(job_id) is not task:
            return
        self._tasks.pop(job_id, None)
        self._cancel_events.pop(job_id, None)
        timer = self._cancel_timers.pop(job_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        permit = self._permits.pop(job_id, None)
        if permit is not None:
            try:
                permit.release()
            except Exception:  # noqa: BLE001 — task cleanup must finish
                logger.warning("job %s permit release failed", job_id, exc_info=True)
        waiters = self._runtime_release_waiters.pop(job_id, set())
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _signal_terminal_waiters(self) -> None:
        for job_id, waiters in list(self._terminal_waiters.items()):
            job = self._jobs.get(job_id)
            if (job is None
                    or job.execution_state not in self._TERMINAL_EXECUTION):
                continue
            self._terminal_waiters.pop(job_id, None)
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(job)

    def _arm_force_cancel(self, job_id: str, task: asyncio.Task) -> None:
        existing = self._cancel_timers.get(job_id)
        if existing is not None and not existing.done():
            return

        async def _cancel_after_grace() -> None:
            await asyncio.sleep(self.CANCEL_GRACE_SECONDS)
            if not task.done():
                task.cancel()

        timer = asyncio.create_task(_cancel_after_grace())
        self._cancel_timers[job_id] = timer

    def _authorize_actor(self, job: VoiceJob, actor: Any) -> None:
        peer = self._actor_value(actor, "creator_peer", "peer")
        scope = self._actor_value(actor, "scope_id", "scope")
        user_id = self._actor_value(actor, "creator_user_id", "user_id")
        if peer != job.creator_peer or scope != job.scope_id:
            raise JobAuthorizationError(f"actor does not own job {job.id!r}")
        if user_id != job.creator_user_id:
            raise JobAuthorizationError(f"actor does not own job {job.id!r}")

    @staticmethod
    def _actor_value(actor: Any, primary: str, fallback: str) -> Any:
        if isinstance(actor, Mapping):
            return actor.get(primary, actor.get(fallback))
        return getattr(actor, primary, getattr(actor, fallback, None))

    @staticmethod
    def _failure_envelope(failure: JobFailure | BaseException) -> JobFailure:
        if isinstance(failure, JobFailure):
            return failure
        if isinstance(failure, asyncio.CancelledError):
            return JobFailure("cancelled", "Delegation cancelled")
        kind = type(failure).__name__
        return JobFailure(kind=kind, message=str(failure))

    def _now(self) -> float:
        return float(self._clock())

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        return None if value is None else float(value)


__all__ = [
    "CancelResult",
    "DeliveryState",
    "ExecutionState",
    "JobAuthorizationError",
    "JobFailure",
    "JobRegistry",
    "JobRegistryError",
    "JobTransitionError",
    "VoiceJob",
]
