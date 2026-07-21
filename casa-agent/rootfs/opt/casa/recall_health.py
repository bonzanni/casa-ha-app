# casa-agent/rootfs/opt/casa/recall_health.py
"""Recall telemetry + circuit breaker for the structured-recall call sites
(personality Task 11).

``observed_recall`` wraps a typed-recall coroutine and records exactly one
telemetry event per outcome (hits / zero_hits / unavailable), re-raising
``RecallUnavailable`` after recording so the three-outcome discipline is
preserved end to end.

``RecallCircuitBreaker`` is a NEW breaker for the four call sites that have no
breaker today (direct-tool, delegated, query_engager, executor-archive). It is
VERIFIED distinct from — and never a replacement for — ``agent.py``'s existing
per-``Agent`` ``_RecallBreaker``, which continues to gate the pre-turn
automatic recall exactly as it does today. Monotonic time is injected so tests
drive recovery without patching ``asyncio.sleep``.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from personality_types import RecallHit
from semantic_memory import RecallUnavailable

RECALL_BREAKER_FAILURE_THRESHOLD = 3
RECALL_BREAKER_RECOVERY_SECONDS = 30.0

# Process-wide telemetry sink cap. This is a continuously-running daemon —
# an unbounded list here would leak forever (one event per recall). Bounded
# to a few thousand: enough for a useful recent-history snapshot, small
# enough to never matter memory-wise (see the pytest-OOM scar tissue in
# CLAUDE.md re: unbounded process-wide state).
RECALL_TELEMETRY_MAX_EVENTS = 4096

RecallPath = Literal["delegated", "direct_tool", "query_engager", "executor_archive"]


@dataclass(frozen=True, slots=True)
class RecallTelemetryEvent:
    path: RecallPath
    outcome: Literal["hits", "zero_hits", "unavailable"]
    reason: str
    latency_ms: int
    hit_count: int


class RecallTelemetry:
    """Append-only in-memory sink of recall outcomes (no gating behaviour)."""

    def __init__(self) -> None:
        self._events: deque[RecallTelemetryEvent] = deque(maxlen=RECALL_TELEMETRY_MAX_EVENTS)

    def record(self, event: RecallTelemetryEvent) -> None:
        self._events.append(event)

    def snapshot(self) -> tuple[RecallTelemetryEvent, ...]:
        return tuple(self._events)


_DEFAULT_TELEMETRY = RecallTelemetry()


def default_telemetry() -> RecallTelemetry:
    """The process-wide telemetry sink the four migrated call sites record to."""
    return _DEFAULT_TELEMETRY


async def observed_recall(
    *, path: RecallPath, telemetry: RecallTelemetry,
    operation: Callable[[], Awaitable[tuple[RecallHit, ...]]],
) -> tuple[RecallHit, ...]:
    """Run ``operation`` (a typed recall), record one telemetry event, and
    return its hits. A ``RecallUnavailable`` (incl. ``RecallProtocolError``) is
    recorded as ``outcome=unavailable`` and RE-RAISED — never swallowed into a
    zero-hit — so callers keep the unavailable-vs-zero-hit distinction."""
    started = time.monotonic()
    try:
        hits = await operation()
    except RecallUnavailable as exc:
        telemetry.record(RecallTelemetryEvent(
            path, "unavailable", exc.reason,
            int((time.monotonic() - started) * 1000), 0,
        ))
        raise
    telemetry.record(RecallTelemetryEvent(
        path, "hits" if hits else "zero_hits", "ok",
        int((time.monotonic() - started) * 1000), len(hits),
    ))
    return hits


class RecallCircuitBreaker:
    """A NEW breaker for the four call sites (direct-tool, delegated,
    query_engager, executor-archive) that have no breaker today — VERIFIED
    distinct from, and never a replacement for, ``agent.py``'s existing
    per-``Agent`` ``_RecallBreaker``, which continues to gate the pre-turn
    automatic recall exactly as it does today."""

    def __init__(
        self, *, failure_threshold: int = RECALL_BREAKER_FAILURE_THRESHOLD,
        recovery_seconds: float = RECALL_BREAKER_RECOVERY_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._recovery = recovery_seconds
        self._now = monotonic
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        if self._opened_at is None:
            return "closed"
        return "open" if self._now() - self._opened_at < self._recovery else "half_open"

    def try_acquire(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open" or self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def failure(self) -> None:
        was_half_open = self.state == "half_open"
        self._probe_in_flight = False
        self._failures += 1
        if was_half_open or self._failures >= self._threshold:
            self._opened_at = self._now()
