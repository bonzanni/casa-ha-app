"""Durable FIFO coordination for proactive Home Assistant voice delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from job_registry import DeliveryState, JobTransitionError, VoiceJob
from voice_job_result import (
    VoiceJobResultError,
    parse_voice_job_result,
    spoken_text_for,
    voice_identity_clearance,
)


logger = logging.getLogger(__name__)

_PROTOCOL = 1
_INBOUND_TYPES = frozenset({
    "job_claimed",
    "job_claim_renew",
    "job_delivery_start",
    "job_playback_started",
    "job_delivered",
    "job_nack",
    "job_revoked",
})
_LIVE_STATES = frozenset({
    DeliveryState.READY,
    DeliveryState.CLAIMED,
    DeliveryState.AUTHORIZED,
    DeliveryState.PLAYING,
})
_BACKGROUND_CAPABILITIES = frozenset({
    "background_jobs", "satellite_announce",
})
_PARK_REASONS = frozenset({
    "satellite_not_found", "satellite_ambiguous",
})


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 512:
        return None
    return normalized


def _is_protocol_one(value: Any) -> bool:
    return type(value) is int and value == _PROTOCOL


@dataclass
class _Attempt:
    job_id: str
    route_id: str
    device_id: str
    attempt_id: str
    endpoint: Any
    offered: bool = True
    revoke_sent: bool = False


class VoiceDeliveryCoordinator:
    """One process-local writer/sweeper over durable delivery CAS state."""

    def __init__(
        self,
        job_registry,
        route_registry,
        *,
        lease_s: float = 15,
        renew_s: float = 5,
        park_s: float = 30,
        clock=time.monotonic,
    ) -> None:
        if (
            lease_s <= 0
            or renew_s <= 0
            or renew_s >= lease_s
            or park_s <= 0
        ):
            raise ValueError("delivery lease/renew windows are invalid")
        self._jobs = job_registry
        self._routes = route_registry
        self._lease_s = float(lease_s)
        self._renew_s = float(renew_s)
        self._park_s = float(park_s)
        self._clock = clock
        # The registry is the durable lease authority.
        self._jobs.LEASE_SECONDS = self._lease_s
        self._attempts: dict[str, _Attempt] = {}
        self._parked_until: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — keep delivery supervised
                # The exception type is useful operational metadata; its
                # message may contain persisted result data and is omitted.
                logger.error(
                    "voice delivery sweep failed error=%s",
                    type(exc).__name__,
                )
            await asyncio.sleep(1)

    async def route_connected(self, endpoint: Any) -> None:
        """Discard unclaimed offers on reconnect and replay durable heads."""
        async with self._lock:
            bound = self._current_bound(endpoint)
            if bound is None:
                return
            for job_id in list(self._parked_until):
                job = self._jobs.get(job_id)
                if job is not None and job.origin_route_id == bound.route_id:
                    self._parked_until.pop(job_id, None)
            for job_id, attempt in list(self._attempts.items()):
                if attempt.route_id != bound.route_id:
                    continue
                if attempt.offered:
                    self._attempts.pop(job_id, None)
                else:
                    # A durable claimed attempt survives reconnect. Rebind its
                    # writer only to the newly authenticated route instance.
                    attempt.endpoint = bound
                    attempt.revoke_sent = False
            await self._recover_due_locked()
            await self._offer_heads_locked()

    async def route_disconnected(self, endpoint: Any) -> None:
        async with self._lock:
            route_id = _identifier(getattr(endpoint, "route_id", None))
            if route_id is None:
                route_id = _identifier(
                    getattr(endpoint, "voice_route_id", None),
                )
            if route_id is None:
                return
            for job_id, attempt in list(self._attempts.items()):
                if attempt.route_id == route_id and attempt.offered:
                    self._attempts.pop(job_id, None)

    async def sweep_once(self) -> None:
        async with self._lock:
            await self._recover_due_locked()
            await self._offer_heads_locked()

    async def handle(self, endpoint: Any, frame: Mapping[str, Any]) -> None:
        frame_type = frame.get("type")
        if (
            frame_type not in _INBOUND_TYPES
            or not _is_protocol_one(frame.get("protocol"))
        ):
            return
        async with self._lock:
            # A frame cannot revive a result or lease whose durable deadline
            # passed between one-second sweep ticks.
            await self._recover_due_locked()
            await self._offer_heads_locked()
            bound = self._current_bound(endpoint)
            job_id = _identifier(frame.get("job_id"))
            attempt_id = _identifier(frame.get("delivery_attempt_id"))
            if bound is None or job_id is None or attempt_id is None:
                await self._deny(endpoint, job_id, attempt_id, "invalid_frame")
                return

            # Revocation acknowledgements refer to the process-local attempt
            # Casa just revoked.  The durable transition may already have
            # cleared its attempt ID (CANCELLED/EXPIRED/READY), so handle the
            # ack before normal durable-state matching.  Duplicate/stale acks
            # are silent to avoid a revoke/ack feedback loop.
            if frame_type == "job_revoked":
                await self._handle_revoked_ack_locked(
                    bound, job_id, attempt_id,
                )
                return

            job = self._jobs.get(job_id)
            if not self._matches_job(bound, job, attempt_id=attempt_id):
                await self._deny(endpoint, job_id, attempt_id, "stale_attempt")
                return

            try:
                if frame_type == "job_claimed":
                    attempt = self._attempts.get(job_id)
                    if (
                        attempt is None
                        or not attempt.offered
                        or attempt.attempt_id != attempt_id
                        or attempt.endpoint is not bound
                    ):
                        await self._deny(
                            endpoint, job_id, attempt_id, "stale_attempt",
                        )
                        return
                    await self._jobs.claim(job_id, attempt_id)
                    attempt.offered = False
                    return

                if frame_type == "job_claim_renew":
                    attempt = self._attempts.get(job_id)
                    if (
                        job.delivery_state is DeliveryState.AUTHORIZED
                        and job.cancel_pending
                        and attempt is not None
                    ):
                        await self._send_revoke(attempt, "cancelled")
                        return
                    await self._jobs.renew(job_id, attempt_id)
                    return

                if frame_type == "job_delivery_start":
                    # Re-run disclosure at the final Casa authorization seam.
                    # The already-offered text is not echoed back across the
                    # wire; authorization is correlated only by the durable
                    # job/attempt pair.
                    self._spoken_text(job)
                    await self._jobs.authorize(job_id, attempt_id)
                    await bound.send_json({
                        "type": "job_delivery_authorized",
                        "protocol": _PROTOCOL,
                        "job_id": job_id,
                        "delivery_attempt_id": attempt_id,
                    })
                    return

                if frame_type == "job_playback_started":
                    await self._jobs.mark_playing(job_id, attempt_id)
                    return

                if frame_type == "job_delivered":
                    await self._jobs.mark_delivered(job_id, attempt_id)
                    self._attempts.pop(job_id, None)
                    await self._offer_heads_locked()
                    return

                if frame_type == "job_nack":
                    reason = frame.get("reason")
                    if not isinstance(reason, str):
                        reason = "integration_nack"
                    await self._jobs.nack(job_id, attempt_id, reason)
                    self._attempts.pop(job_id, None)
                    if reason in _PARK_REASONS:
                        self._parked_until[job_id] = (
                            self._clock() + self._park_s
                        )
                    await self._offer_heads_locked()
                    return
            except JobTransitionError:
                await self._deny(endpoint, job_id, attempt_id, "stale_attempt")

    async def _recover_due_locked(self) -> None:
        await self._jobs.expire_due()
        await self._jobs.expire_leases()
        self._release_due_parks_locked()
        self._restore_durable_attempts_locked()
        await self._reconcile_attempts_locked()

    def _release_due_parks_locked(self) -> None:
        now = self._clock()
        for job_id, deadline in list(self._parked_until.items()):
            job = self._jobs.get(job_id)
            if (
                deadline <= now
                or job is None
                or job.delivery_state is not DeliveryState.READY
            ):
                self._parked_until.pop(job_id, None)

    def _restore_durable_attempts_locked(self) -> None:
        for job in self._jobs.all():
            if (
                job.delivery_state not in {
                    DeliveryState.CLAIMED,
                    DeliveryState.AUTHORIZED,
                    DeliveryState.PLAYING,
                }
                or not job.origin_route_id
                or not job.origin_device_id
                or not job.delivery_attempt_id
                or job.lease_until is None
            ):
                continue
            existing = self._attempts.get(job.id)
            if (
                existing is not None
                and not existing.offered
                and existing.attempt_id == job.delivery_attempt_id
                and existing.route_id == job.origin_route_id
                and existing.device_id == job.origin_device_id
            ):
                continue
            route = self._routes.get_connected(job.origin_route_id)
            endpoint = (
                route if route is not None and self._current_bound(route)
                else None
            )
            self._attempts[job.id] = _Attempt(
                job_id=job.id,
                route_id=job.origin_route_id,
                device_id=job.origin_device_id,
                attempt_id=job.delivery_attempt_id,
                endpoint=endpoint,
                offered=False,
            )

    async def _handle_revoked_ack_locked(
        self,
        bound: Any,
        job_id: str,
        attempt_id: str,
    ) -> None:
        attempt = self._attempts.get(job_id)
        if (
            attempt is None
            or attempt.attempt_id != attempt_id
            or attempt.route_id != bound.route_id
            or attempt.endpoint is not bound
            or not attempt.revoke_sent
        ):
            return
        job = self._jobs.get(job_id)
        if (
            job is not None
            and job.delivery_state is DeliveryState.AUTHORIZED
            and job.cancel_pending
            and job.delivery_attempt_id == attempt_id
        ):
            try:
                await self._jobs.nack(
                    job_id, attempt_id, "preempted_before_playback",
                )
            except JobTransitionError:
                return
        self._attempts.pop(job_id, None)
        await self._offer_heads_locked()

    def _current_bound(self, endpoint: Any) -> Any | None:
        route_id = _identifier(getattr(endpoint, "route_id", None))
        if route_id is None:
            route_id = _identifier(getattr(endpoint, "voice_route_id", None))
        if route_id is None:
            return None
        current = self._routes.get_connected(route_id)
        if current is endpoint:
            bound = current
        elif getattr(current, "connection", None) is endpoint:
            bound = current
        else:
            return None
        capabilities = getattr(bound, "capabilities", None)
        if capabilities is None:
            capabilities = getattr(bound, "voice_route_capabilities", ())
        if not isinstance(capabilities, (set, frozenset, list, tuple)):
            return None
        if not _BACKGROUND_CAPABILITIES <= frozenset(capabilities):
            return None
        return bound

    def _matches_job(
        self,
        bound: Any,
        job: VoiceJob | None,
        *,
        attempt_id: str,
    ) -> bool:
        if job is None or job.origin_route_id != bound.route_id:
            return False
        if not job.origin_device_id:
            return False
        attempt = self._attempts.get(job.id)
        if attempt is not None and (
            attempt.route_id != job.origin_route_id
            or attempt.device_id != job.origin_device_id
        ):
            return False
        if job.delivery_state is DeliveryState.READY:
            return bool(
                attempt is not None
                and attempt.offered
                and attempt.endpoint is bound
                and attempt.attempt_id == attempt_id
            )
        return bool(
            attempt is not None
            and not attempt.offered
            and attempt.endpoint is bound
            and attempt.attempt_id == attempt_id
            and job.delivery_state in _LIVE_STATES
            and job.delivery_attempt_id == attempt_id
        )

    async def _reconcile_attempts_locked(self) -> None:
        for job_id, attempt in list(self._attempts.items()):
            job = self._jobs.get(job_id)
            if job is not None and (
                (attempt.offered and job.delivery_state is DeliveryState.READY)
                or (
                    not attempt.offered
                    and job.delivery_state in {
                        DeliveryState.CLAIMED,
                        DeliveryState.AUTHORIZED,
                        DeliveryState.PLAYING,
                    }
                    and job.delivery_attempt_id == attempt.attempt_id
                )
            ):
                if job.cancel_pending:
                    if not attempt.revoke_sent:
                        attempt.revoke_sent = await self._send_revoke(
                            attempt, "cancelled",
                        )
                continue
            if not attempt.revoke_sent:
                attempt.revoke_sent = await self._send_revoke(
                    attempt, "no_longer_deliverable",
                )

    async def _offer_heads_locked(self) -> None:
        occupied: set[str] = set()
        for job in self._jobs.all():
            if (
                job.delivery_state not in _LIVE_STATES
                or not job.origin_route_id
                or not job.origin_device_id
            ):
                continue
            key = job.origin_device_id
            if key in occupied:
                continue
            occupied.add(key)
            if job.delivery_state is not DeliveryState.READY:
                continue
            if job.id in self._parked_until:
                continue
            existing = self._attempts.get(job.id)
            if existing is not None and existing.offered:
                continue
            route = self._routes.get_connected(job.origin_route_id)
            if route is None or self._current_bound(route) is None:
                continue
            attempt = _Attempt(
                job_id=job.id,
                route_id=job.origin_route_id,
                device_id=job.origin_device_id,
                attempt_id=str(uuid.uuid4()),
                endpoint=route,
            )
            spoken_text = self._spoken_text(job)
            self._attempts[job.id] = attempt
            sent = await self._safe_send(route, {
                "type": "job_ready",
                "protocol": _PROTOCOL,
                "job_id": job.id,
                "delivery_attempt_id": attempt.attempt_id,
                "route_id": job.origin_route_id,
                "origin_device_id": job.origin_device_id,
                "spoken_text": spoken_text,
                "ready_at": job.terminal_at,
                "expires_at": job.expires_at,
                "delivery_sequence": job.delivery_sequence,
            })
            if not sent:
                self._attempts.pop(job.id, None)

    def _spoken_text(self, job: VoiceJob) -> str:
        if job.result is not None:
            try:
                payload = json.loads(job.result)
                result = parse_voice_job_result(payload)
                return spoken_text_for(
                    result,
                    prompted=False,
                    identity_clearance=voice_identity_clearance({
                        "channel": "voice",
                    }),
                )
            except (json.JSONDecodeError, TypeError, VoiceJobResultError):
                pass
        return "The specialist job could not be completed."

    async def _send_revoke(self, attempt: _Attempt, reason: str) -> bool:
        current = self._routes.get_connected(attempt.route_id)
        if current is None or current is not attempt.endpoint:
            return False
        return await self._safe_send(current, {
            "type": "job_revoke",
            "protocol": _PROTOCOL,
            "job_id": attempt.job_id,
            "delivery_attempt_id": attempt.attempt_id,
            "reason": reason,
        })

    async def _deny(
        self,
        endpoint: Any,
        job_id: str | None,
        attempt_id: str | None,
        reason: str,
    ) -> None:
        await self._safe_send(endpoint, {
            "type": "job_revoke",
            "protocol": _PROTOCOL,
            "job_id": job_id,
            "delivery_attempt_id": attempt_id,
            "reason": reason,
        })

    @staticmethod
    async def _safe_send(endpoint: Any, frame: dict[str, Any]) -> bool:
        try:
            await endpoint.send_json(frame)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never log result/spoken payloads
            logger.warning(
                "voice delivery frame send failed type=%s job=%s",
                frame.get("type"), str(frame.get("job_id") or "-")[:8],
            )
            return False


__all__ = ["VoiceDeliveryCoordinator"]
