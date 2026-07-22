"""Personality Task 11 follow-up (GH #200): the per-path
``recall_health.RecallCircuitBreaker`` must actually GATE the four on-demand
recall call sites via ``observed_recall`` — before this fix it was wired for
telemetry only and never fast-failed anything.

Distinct from ``agent.py``'s own per-``Agent`` ``_RecallBreaker`` (the
every-turn auto-recall breaker) — untouched here.

The autouse fixture resets ``recall_health``'s process-wide per-path breaker
registry around every test in THIS file only (never repo-wide) via
``recall_health.reset_recall_breakers()``, so breaker state opened by one test
can never leak into another test — in this file or any other.
"""
from __future__ import annotations

import pytest

from personality_types import RecallHit
from semantic_memory import RecallUnavailable

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _reset_recall_breakers():
    from recall_health import reset_recall_breakers

    reset_recall_breakers()
    yield
    reset_recall_breakers()


def _hit(text: str = "fact") -> RecallHit:
    return RecallHit(
        text=text, memory_type="world", sensitivity="friends",
        application_tags=(), provenance=None, backend_id="b1", document_id=None,
        chunk_id=None, source_fact_ids=None, metadata=None, context=None,
        score=None,
    )


class _Clock:
    """Injectable monotonic clock — never patch asyncio.sleep / time.monotonic."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ---------------------------------------------------------------------------
# (a) opens after threshold, fast-fails WITHOUT invoking the wrapped callable
# ---------------------------------------------------------------------------


async def test_breaker_opens_after_threshold_and_fast_fails_without_invoking_callable():
    from recall_health import RecallCircuitBreaker, RecallTelemetry, _PATH_BREAKERS, observed_recall

    clock = _Clock()
    _PATH_BREAKERS["direct_tool"] = RecallCircuitBreaker(
        failure_threshold=3, recovery_seconds=30.0, monotonic=clock,
    )
    tel = RecallTelemetry()
    calls = 0

    async def _failing():
        nonlocal calls
        calls += 1
        raise RecallUnavailable("http_504")

    for _ in range(3):
        with pytest.raises(RecallUnavailable):
            await observed_recall(path="direct_tool", telemetry=tel, operation=_failing)
    assert calls == 3

    # Breaker is now OPEN: the next call must fast-fail WITHOUT ever invoking
    # the wrapped callable again.
    with pytest.raises(RecallUnavailable) as excinfo:
        await observed_recall(path="direct_tool", telemetry=tel, operation=_failing)
    assert calls == 3  # unchanged — operation was NOT invoked
    assert excinfo.value.reason == "circuit_open"

    events = tel.snapshot()
    assert events[-1].path == "direct_tool"
    assert events[-1].outcome == "unavailable"
    assert events[-1].reason == "circuit_open"


# ---------------------------------------------------------------------------
# (d) fast-fail surfaces as the unavailable type, never as an empty result
# ---------------------------------------------------------------------------


async def test_fast_fail_is_recall_unavailable_never_an_empty_tuple():
    from recall_health import RecallCircuitBreaker, RecallTelemetry, _PATH_BREAKERS, observed_recall

    clock = _Clock()
    _PATH_BREAKERS["query_engager"] = RecallCircuitBreaker(
        failure_threshold=1, recovery_seconds=30.0, monotonic=clock,
    )
    tel = RecallTelemetry()

    async def _failing():
        raise RecallUnavailable("transport")

    with pytest.raises(RecallUnavailable):
        await observed_recall(path="query_engager", telemetry=tel, operation=_failing)

    # OPEN now. Confirm the fast-fail is a real RecallUnavailable, not "()".
    with pytest.raises(RecallUnavailable) as excinfo:
        await observed_recall(path="query_engager", telemetry=tel, operation=_failing)
    assert isinstance(excinfo.value, RecallUnavailable)
    assert excinfo.value.reason == "circuit_open"


# ---------------------------------------------------------------------------
# (b) success (incl. zero-hit) closes/recovers per the primitive's policy
# ---------------------------------------------------------------------------


async def test_success_recovers_breaker_after_cooldown_and_zero_hit_counts_as_success():
    from recall_health import RecallCircuitBreaker, RecallTelemetry, _PATH_BREAKERS, observed_recall

    clock = _Clock()
    _PATH_BREAKERS["delegated"] = RecallCircuitBreaker(
        failure_threshold=2, recovery_seconds=30.0, monotonic=clock,
    )
    tel = RecallTelemetry()

    async def _failing():
        raise RecallUnavailable("timeout")

    for _ in range(2):
        with pytest.raises(RecallUnavailable):
            await observed_recall(path="delegated", telemetry=tel, operation=_failing)

    # Confirm OPEN before cooldown.
    with pytest.raises(RecallUnavailable) as excinfo:
        await observed_recall(path="delegated", telemetry=tel, operation=_failing)
    assert excinfo.value.reason == "circuit_open"

    # Cooldown elapses -> a single half-open probe is admitted.
    clock.advance(31.0)

    async def _zero_hit():
        return ()

    hits = await observed_recall(path="delegated", telemetry=tel, operation=_zero_hit)
    assert hits == ()  # a genuine zero-hit IS success, and it must go through

    events = tel.snapshot()
    assert events[-1].outcome == "zero_hits"

    # The breaker is closed again: ONE subsequent failure (below the
    # threshold of 2) must not reopen it — proving success() actually reset
    # the failure count/opened_at, not just admitted a single probe.
    async def _one_failure():
        raise RecallUnavailable("transport")

    with pytest.raises(RecallUnavailable):
        await observed_recall(path="delegated", telemetry=tel, operation=_one_failure)

    hits2 = await observed_recall(path="delegated", telemetry=tel, operation=_zero_hit)
    assert hits2 == ()


# ---------------------------------------------------------------------------
# (c) paths are independent
# ---------------------------------------------------------------------------


async def test_paths_are_independent_one_open_breaker_does_not_block_another():
    from recall_health import RecallCircuitBreaker, RecallTelemetry, _PATH_BREAKERS, observed_recall

    clock = _Clock()
    _PATH_BREAKERS["query_engager"] = RecallCircuitBreaker(
        failure_threshold=1, recovery_seconds=30.0, monotonic=clock,
    )
    tel = RecallTelemetry()

    async def _failing():
        raise RecallUnavailable("timeout")

    with pytest.raises(RecallUnavailable):
        await observed_recall(path="query_engager", telemetry=tel, operation=_failing)

    # query_engager is now OPEN.
    with pytest.raises(RecallUnavailable) as excinfo:
        await observed_recall(path="query_engager", telemetry=tel, operation=_failing)
    assert excinfo.value.reason == "circuit_open"

    # executor_archive (and direct_tool/delegated) are untouched — still
    # closed, and their calls go through normally.
    calls = 0

    async def _ok():
        nonlocal calls
        calls += 1
        return (_hit(),)

    hits = await observed_recall(path="executor_archive", telemetry=tel, operation=_ok)
    assert calls == 1
    assert len(hits) == 1

    hits2 = await observed_recall(path="direct_tool", telemetry=tel, operation=_ok)
    assert calls == 2
    assert len(hits2) == 1


# ---------------------------------------------------------------------------
# (e) the reset seam restores clean state
# ---------------------------------------------------------------------------


def test_reset_seam_clears_the_registry():
    from recall_health import RecallCircuitBreaker, _PATH_BREAKERS, reset_recall_breakers

    br = RecallCircuitBreaker(failure_threshold=1)
    _PATH_BREAKERS["direct_tool"] = br
    br.try_acquire()
    br.failure()
    assert br.state == "open"
    assert "direct_tool" in _PATH_BREAKERS

    reset_recall_breakers()
    assert _PATH_BREAKERS == {}


async def test_a_fresh_breaker_after_reset_starts_closed_pristine_default():
    """Relies on the autouse fixture's PRE-test reset: even though earlier
    tests in this module opened breakers for "direct_tool"/"query_engager"/
    "delegated", this test sees a pristine registry and its call must go
    through (no leakage across tests)."""
    from recall_health import RecallTelemetry, observed_recall

    tel = RecallTelemetry()

    async def _ok():
        return ()

    hits = await observed_recall(path="direct_tool", telemetry=tel, operation=_ok)
    assert hits == ()
    assert tel.snapshot()[-1].outcome == "zero_hits"
