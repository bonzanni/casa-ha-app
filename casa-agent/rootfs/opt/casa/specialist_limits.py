"""Specialist concurrency limiter + per-role delegation telemetry (spec §4.6).

A voice turn budget (Task 4's ``_voice_wait_from_deadline`` in ``tools.py``)
bounds the *duration* of one delegated turn — it does nothing to bound how
many delegations can be running at once. Without a real concurrency cap, an
unauthenticated room mic could fire ``delegate_to_agent`` repeatedly and
spawn unbounded, simultaneously-billing specialist runs. This module adds
the two real limits ``tools._prelaunch`` enforces BEFORE any launch side
effect (record, task, driver start):

* a GLOBAL cap (``specialist_max_concurrency``, default 2) on delegations
  in flight across the whole fleet at once;
* a HARD per-scope cap of exactly 1 — **not configurable** — one active
  delegation per scope. "Scope" is the calling session: the voice channel's
  ``channels/voice/channel.py`` already keys its own per-turn rate limiter
  off a ``scope_id`` and threads that SAME value through as
  ``origin["chat_id"]`` for every voice turn (see ``_resolve_scope_id`` /
  the ``"chat_id": scope_id`` origin fields there); other channels' chat_id
  serves the same "one calling session" role. ``tools._delegation_scope``
  combines that session id with the target agent name, so the cap reads
  literally as spec §4.6's "one active MTG delegation per voice scope" —
  a given session may not have two delegations to the SAME specialist in
  flight at once (concurrent delegations to two DIFFERENT specialists from
  the same session are still allowed, gated only by the global cap).

Usage (see ``tools.py``'s ``_prelaunch``/``delegate_to_agent`` wiring):

    permit = limiter.try_acquire(scope)   # in _prelaunch, after requires
    if permit is None:
        ...deny as typed "busy", no side effects yet...

The permit must be released on EVERY terminal path of the delegated
operation. Because ``Permit.release()`` is idempotent and cancellation-safe,
the wiring layers several release hooks that all target the same permit —
whichever fires first wins, the rest are no-ops:

* a lexical ``owned`` try/finally in ``delegate_to_agent`` spanning the
  acquire→launch region (catches CancelledError during any await BEFORE
  ownership transfers to a task/record — the single most common leak class);
* a ``_permit_release_callback`` done-callback on the sync/async task
  (fires even for a task cancelled before its coroutine ever runs — which
  has no coroutine ``finally``);
* release inside EVERY registry terminal transition (``mark_error``,
  ``mark_cancelled``, ``mark_completed``, ``try_transition_terminal``,
  ``complete``/``fail``/``cancel_delegation``) — so direct terminal routes
  that bypass ``_finalize_engagement`` (resume/orphan failures) still free
  the slot;
* ``_finalize_engagement`` as an idempotent fallback for interactive.

Cost/usage telemetry is split from launch counting (see ``SpecialistTelemetry``):
``record_launch`` counts at ownership transfer (so setup failures still
count); ``record_cost`` aggregates only when a ``ResultMessage`` is observed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input/output bounds (spec §4.6) — conservative defaults; not yet
# operator-configurable (Task 7 wires real HA options for the concurrency
# knobs; these char bounds stay fixed pending that follow-up).
# ---------------------------------------------------------------------------

_MAX_TASK_CHARS: int = 4_000
_MAX_CONTEXT_CHARS: int = 8_000
_MAX_OUTPUT_CHARS: int = 20_000


def truncate_output(text: str) -> tuple[str, bool]:
    """Bound a delegated agent's output to ``_MAX_OUTPUT_CHARS`` (spec §4.6).

    Returns ``(text, output_truncated)`` — the (possibly shortened) text and
    a flag the caller propagates to its wire result / notification so a
    consumer knows the answer was clipped (voice TTS especially). Read
    ``_MAX_OUTPUT_CHARS`` off this module at call time so a test monkeypatch
    of the cap is honoured."""
    if len(text) > _MAX_OUTPUT_CHARS:
        return text[:_MAX_OUTPUT_CHARS], True
    return text, False


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class Permit:
    """A held concurrency slot: one global + one per-scope.

    ``release()`` is idempotent and cancellation-safe — it may be called
    more than once (only the first call has any effect) and may run from
    inside a ``finally`` block that executes because the owning task was
    cancelled. Both properties are load-bearing: a leaked permit
    permanently blocks its scope, and a double-decrement would silently
    let more than ``max_global`` delegations run at once.

    Never constructed directly — obtained from
    :meth:`SpecialistLimiter.try_acquire`.
    """

    __slots__ = ("_limiter", "_scope", "_released")

    def __init__(self, limiter: "SpecialistLimiter", scope: str) -> None:
        self._limiter = limiter
        self._scope = scope
        self._released = False

    def release(self) -> None:
        # No lock needed: Casa is single-threaded asyncio, and this method
        # contains no `await`, so the check-then-set is atomic with respect
        # to every other coroutine (including a concurrent cancellation
        # unwinding through the SAME finally block re-entrantly, which
        # cannot happen — a task's own finally runs once).
        if self._released:
            return
        self._released = True
        self._limiter._release(self._scope)

    @property
    def scope(self) -> str:
        return self._scope


class SpecialistLimiter:
    """Bounds specialist-delegation concurrency (spec §4.6).

    ``max_global`` is the total number of delegations allowed in flight
    across every scope at once (``specialist_max_concurrency`` option,
    default 2). The per-scope cap is always exactly 1 and is NOT a
    constructor parameter — it is the hard "one active delegation per
    scope" invariant the spec requires.

    Implementation note: rather than a ``dict[scope, Semaphore(1)]`` that
    would grow forever (a semaphore entry created for every distinct scope
    ever seen, never removed), active scopes are tracked in a plain
    ``set`` that self-cleans on release — memory stays O(concurrently
    active scopes), not O(all scopes ever seen).
    """

    def __init__(self, max_global: int) -> None:
        if max_global < 1:
            raise ValueError(f"max_global must be >= 1, got {max_global!r}")
        self._max_global = max_global
        self._global_count = 0
        self._active_scopes: set[str] = set()

    def try_acquire(self, scope: str) -> "Permit | None":
        """Attempt to acquire one global + one per-scope slot for *scope*.

        Returns a :class:`Permit` on success, ``None`` if either cap is
        already saturated (a "busy" denial). The per-scope check runs
        FIRST — if the scope is already active, the global slot is never
        touched, so a per-scope-full denial never leaks a global count.
        """
        if scope in self._active_scopes:
            return None  # per-scope cap (hard 1) already held
        if self._global_count >= self._max_global:
            return None  # global cap saturated
        self._active_scopes.add(scope)
        self._global_count += 1
        return Permit(self, scope)

    def _release(self, scope: str) -> None:
        self._active_scopes.discard(scope)
        if self._global_count > 0:
            self._global_count -= 1

    @property
    def in_flight(self) -> int:
        """Current global in-flight count. Test/observability helper."""
        return self._global_count


# ---------------------------------------------------------------------------
# Per-role telemetry
# ---------------------------------------------------------------------------


@dataclass
class RoleStats:
    """Cumulative counters for one delegated role (spec §4.6)."""

    delegations: int = 0
    denials: int = 0
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


class SpecialistTelemetry:
    """Per-role delegation/cost counters + threshold alerting (spec §4.6).

    ``cost_alert_threshold`` (``specialist_cost_alert_threshold`` option) is
    the cumulative per-role USD figure past which every further
    :meth:`record_delegation` call for that role logs a WARNING in
    addition to the normal INFO line. ``None`` disables alerting.
    """

    def __init__(self, cost_alert_threshold: float | None = None) -> None:
        self._threshold = cost_alert_threshold
        self._stats: dict[str, RoleStats] = {}

    def _stats_for(self, role: str) -> RoleStats:
        return self._stats.setdefault(role, RoleStats())

    def record_launch(self, role: str) -> None:
        """Count ONE delegation launch to *role* (spec §4.6).

        Counting is deliberately separate from cost aggregation
        (:meth:`record_cost`): a launch is counted at the moment the caller
        transfers ownership of the running work (task created / driver
        started), BEFORE the delegated turn does its own setup (memory
        recall, options build) — so a delegation that fails DURING that
        setup, or whose SDK ``ResultMessage`` never arrives, is still
        counted. Cost/usage is aggregated later, only if a ``ResultMessage``
        is actually observed."""
        stats = self._stats_for(role)
        stats.delegations += 1
        logger.info(
            "specialist_telemetry_launch role=%s delegations=%d",
            role, stats.delegations,
        )

    def record_cost(
        self, role: str, *, cost_usd: float = 0.0, usage: dict | None = None,
    ) -> None:
        """Aggregate cost/usage from ONE delegated turn's SDK
        ``ResultMessage`` (spec §4.6). Does NOT increment the delegation
        count — see :meth:`record_launch`.

        ``usage`` is the SDK ``ResultMessage.usage`` dict (or the
        ``tokens.extract_usage`` normalized form) — only
        ``input_tokens``/``output_tokens`` are aggregated; unknown/missing
        keys default to 0 so a malformed usage payload never raises."""
        usage = usage or {}
        stats = self._stats_for(role)
        cost_usd = float(cost_usd or 0.0)
        stats.total_cost_usd += cost_usd
        try:
            stats.total_input_tokens += int(usage.get("input_tokens", 0) or 0)
            stats.total_output_tokens += int(usage.get("output_tokens", 0) or 0)
        except (TypeError, ValueError):
            pass
        logger.info(
            "specialist_telemetry_cost role=%s cost_usd=%.4f "
            "total_cost_usd=%.4f in_tok=%d out_tok=%d",
            role, cost_usd, stats.total_cost_usd,
            stats.total_input_tokens, stats.total_output_tokens,
        )
        if self._threshold is not None and stats.total_cost_usd > self._threshold:
            logger.warning(
                "specialist_cost_alert role=%s total_cost_usd=%.4f exceeds "
                "specialist_cost_alert_threshold=%.4f (spec §4.6)",
                role, stats.total_cost_usd, self._threshold,
            )

    def record_denial(self, role: str, *, kind: str) -> None:
        """Record a pre-launch denial (busy / input_too_large / ...)."""
        stats = self._stats_for(role)
        stats.denials += 1
        logger.info(
            "specialist_telemetry_denial role=%s kind=%s denials=%d",
            role, kind, stats.denials,
        )

    def snapshot(self, role: str) -> RoleStats:
        """Return a copy of *role*'s counters (default zeros if unseen)."""
        existing = self._stats.get(role)
        return RoleStats(**vars(existing)) if existing is not None else RoleStats()
