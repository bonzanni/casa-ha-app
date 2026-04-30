"""Tests for CachedMemoryProvider — butler's low-latency wrapper."""

from __future__ import annotations

import asyncio

import pytest

from memory import CachedMemoryProvider, MemoryProvider

pytestmark = pytest.mark.asyncio


class RecordingProvider(MemoryProvider):
    """Backend whose calls we can count and whose responses we can queue."""

    def __init__(self) -> None:
        self.get_calls = 0
        self.add_calls = 0
        self.ensure_calls = 0
        self.cross_calls = 0
        self.overlay_calls = 0
        self._queue: list[str] = []
        self._error_on_get: Exception | None = None

    def queue(self, *responses: str) -> None:
        self._queue.extend(responses)

    async def ensure_session(self, session_id, agent_role, user_peer="nicola"):
        self.ensure_calls += 1

    async def get_context(self, session_id, tokens, search_query=None):
        if self._error_on_get is not None:
            raise self._error_on_get
        self.get_calls += 1
        if self._queue:
            return self._queue.pop(0)
        return f"ctx#{self.get_calls}"

    async def peer_overlay_context(
        self, observer_role, user_peer, search_query, tokens,
    ):
        self.overlay_calls += 1
        return ""

    async def add_turn(
        self, session_id, agent_role, user_text, assistant_text,
        user_peer="nicola",
    ):
        self.add_calls += 1

    async def cross_peer_context(
        self, observer_role, query, tokens, user_peer="nicola",
    ):
        self.cross_calls += 1
        return ""


async def _drain():
    """Let queued background tasks run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_first_call_fetches_subsequent_calls_cached():
    backend = RecordingProvider()
    backend.queue("first")
    cached = CachedMemoryProvider(backend)

    out1 = await cached.get_context("s", 100)
    out2 = await cached.get_context("s", 100)

    assert out1 == "first"
    assert out2 == "first"
    assert backend.get_calls == 1


async def test_search_query_does_not_participate_in_cache_key():
    backend = RecordingProvider()
    backend.queue("first")
    cached = CachedMemoryProvider(backend)

    await cached.get_context("s", 100, search_query="a")
    await cached.get_context("s", 100, search_query="b")

    assert backend.get_calls == 1


async def test_distinct_keys_fetch_separately():
    backend = RecordingProvider()
    backend.queue("a", "b", "c")
    cached = CachedMemoryProvider(backend)

    await cached.get_context("s1", 100)
    await cached.get_context("s2", 100)
    await cached.get_context("s1", 100)

    assert backend.get_calls == 3


async def test_add_turn_triggers_background_refresh():
    backend = RecordingProvider()
    backend.queue("v1", "v2")
    cached = CachedMemoryProvider(backend)

    await cached.get_context("s", 100)
    await cached.add_turn("s", "assistant", "u", "a")
    await _drain()

    out = await cached.get_context("s", 100)
    assert out == "v2"
    assert backend.get_calls == 2


async def test_refresh_error_does_not_crash_and_keeps_stale_cache(caplog):
    import logging

    backend = RecordingProvider()
    backend.queue("v1")
    cached = CachedMemoryProvider(backend)

    await cached.get_context("s", 100)
    backend._error_on_get = RuntimeError("boom")

    with caplog.at_level(logging.WARNING):
        await cached.add_turn("s", "assistant", "u", "a")
        await _drain()

    # stale cache preserved, error logged
    out = await cached.get_context("s", 100)
    assert out == "v1"
    assert any("refresh" in r.message.lower() for r in caplog.records)


async def test_ensure_session_passes_through():
    backend = RecordingProvider()
    cached = CachedMemoryProvider(backend)
    await cached.ensure_session("s", "assistant")
    assert backend.ensure_calls == 1


async def test_add_turn_passes_through():
    backend = RecordingProvider()
    cached = CachedMemoryProvider(backend)
    await cached.add_turn("s", "assistant", "u", "a")
    await _drain()
    assert backend.add_calls == 1


async def test_background_task_strongly_retained_during_flight():
    backend = RecordingProvider()
    backend.queue("v1")
    cached = CachedMemoryProvider(backend)
    await cached.get_context("s", 100)

    # Trigger refresh
    await cached.add_turn("s", "assistant", "u", "a")
    # Before draining, the set holds the task.
    assert len(cached._bg_tasks) == 1
    await _drain()
    # Done callback removed it.
    assert len(cached._bg_tasks) == 0


class TestColdKeyDedup:
    async def test_concurrent_cold_reads_same_key_hit_backend_once(self):
        """10 concurrent get_context on same key → 1 backend call, 10 same returns."""
        backend = RecordingProvider()
        backend.queue("shared")
        cached = CachedMemoryProvider(backend)

        results = await asyncio.gather(
            *(cached.get_context("s", 100) for _ in range(10)),
        )

        assert backend.get_calls == 1
        assert all(r == "shared" for r in results)
        assert cached._cache[("s", "assistant", 100)] == "shared"

    async def test_concurrent_cold_reads_distinct_keys_parallelize(self):
        """Each backend call sleeps 100 ms; 5 distinct keys in ~100 ms, not ~500."""
        import time

        class SlowProvider(RecordingProvider):
            async def get_context(self, session_id, tokens, search_query=None):
                await asyncio.sleep(0.1)
                return await super().get_context(
                    session_id, tokens, search_query=search_query,
                )

        backend = SlowProvider()
        backend.queue("a", "b", "c", "d", "e")
        cached = CachedMemoryProvider(backend)

        t0 = time.perf_counter()
        await asyncio.gather(
            cached.get_context("s1", 100),
            cached.get_context("s2", 100),
            cached.get_context("s3", 100),
            cached.get_context("s4", 100),
            cached.get_context("s5", 100),
        )
        elapsed = time.perf_counter() - t0

        assert backend.get_calls == 5
        assert elapsed < 0.3, f"expected ~0.1s, got {elapsed:.2f}s"

    async def test_second_caller_awaits_in_flight_backend_call(self):
        """Second concurrent cold reader blocks on lock; no second backend call."""
        gate = asyncio.Event()
        started = asyncio.Event()

        class GatedProvider(RecordingProvider):
            async def get_context(self, session_id, tokens, search_query=None):
                self.get_calls += 1
                started.set()
                await gate.wait()
                return self._queue.pop(0)

        backend = GatedProvider()
        backend.queue("only")
        cached = CachedMemoryProvider(backend)

        task_a = asyncio.create_task(cached.get_context("s", 100))
        await started.wait()
        # First call is now inside the backend; start the second — it must
        # block on the key's lock, not launch a second backend call.
        task_b = asyncio.create_task(cached.get_context("s", 100))
        # Give task_b a chance to hit the lock.
        for _ in range(5):
            await asyncio.sleep(0)
        assert backend.get_calls == 1, "second caller should have blocked, not hit backend"

        gate.set()
        a, b = await asyncio.gather(task_a, task_b)
        assert a == b == "only"
        assert backend.get_calls == 1

    async def test_locks_dict_is_bounded_by_distinct_keys(self):
        """Locks dict grows only with distinct (session_id, role, tokens) triples."""
        backend = RecordingProvider()
        backend.queue("a", "a2", "b2")
        cached = CachedMemoryProvider(backend)

        await cached.get_context("s", 100)
        await cached.get_context("s", 100)  # reuses lock
        await cached.get_context("s", 200)
        await cached.get_context("s2", 100)

        assert backend.get_calls == 3
        assert len(cached._locks) == 3, (
            f"expected 3 distinct lock keys, got {len(cached._locks)}: "
            f"{list(cached._locks.keys())}"
        )


async def test_get_context_cache_miss_does_not_emit_wrapper_log(caplog):
    """M3b — on cache miss, CachedMemoryProvider does NOT emit its own
    memory_call. The inner backend's emission carries the call.
    Asserting this prevents double-counting in operators' rate dashboards."""
    import logging
    inner = RecordingProvider()
    inner.queue("ctx-A")
    cached = CachedMemoryProvider(inner)

    with caplog.at_level(logging.INFO, logger="memory"):
        await cached.get_context("sid", tokens=100)

    # RecordingProvider is a test stub and does NOT emit memory_call —
    # only real providers (Honcho/SQLite) do. So the count should be 0
    # with this stub. The contract under test here is "wrapper does not
    # emit on miss" — if a future change makes the wrapper emit a miss
    # line, this test catches it.
    wrapper_records = [
        r for r in caplog.records
        if r.message == "memory_call" and getattr(r, "cache_hit", None) is False
    ]
    assert len(wrapper_records) == 0, (
        f"wrapper emitted memory_call on cache miss: {wrapper_records}"
    )


async def test_get_context_cache_hit_emits_memory_call(caplog):
    """M3b — on cache hit, the wrapper emits its own memory_call line
    with cache_hit=True, backend = inner backend's class-derived name,
    t_ms ~ 0, and peer_count / summary_present / peer_repr_present None
    (we don't re-derive these on hit because the cache stores only the
    rendered string)."""
    import logging
    inner = RecordingProvider()
    inner.queue("ctx-A")
    cached = CachedMemoryProvider(inner)

    # Prime the cache (miss path).
    await cached.get_context("sid", tokens=100)

    # Now the hit path — this is the one we're asserting.
    with caplog.at_level(logging.INFO, logger="memory"):
        await cached.get_context("sid", tokens=100)

    records = [r for r in caplog.records if r.message == "memory_call"]
    assert len(records) == 1, (
        f"expected exactly 1 memory_call (the cache-hit emission); "
        f"got {len(records)}: {records}"
    )
    rec = records[0]
    assert rec.cache_hit is True
    # backend name is derived from the inner provider's class name.
    # RecordingProvider is the test stub here.
    assert rec.backend == "recording"
    assert rec.session_id == "sid"
    assert rec.agent_role == "role"
    assert isinstance(rec.t_ms, int) and rec.t_ms >= 0
    assert rec.peer_count is None
    assert rec.summary_present is None
    assert rec.peer_repr_present is None
    # M6 § 9 — call_type field distinguishes self vs cross_peer reads
    assert getattr(rec, "call_type", None) == "self"


async def test_cached_provider_passes_through_cross_peer_context():
    """M6 § 5.2: CachedMemoryProvider does NOT cache cross-peer reads
    (search_query would have to be the cache key, defeating the
    purpose). Behavior is plain passthrough — no cache mutation,
    no double-emit."""
    from memory import CachedMemoryProvider

    class RecordingBackend:
        def __init__(self):
            self.calls: list[tuple] = []
        async def ensure_session(self, *a, **kw): ...
        async def get_context(self, *a, **kw): return ""
        async def add_turn(self, *a, **kw): ...
        async def cross_peer_context(
            self, observer_role, query, tokens, user_peer="nicola",
        ):
            self.calls.append(
                (observer_role, query, tokens, user_peer)
            )
            return f"<<{observer_role}/{query}>>"

    backend = RecordingBackend()
    cached = CachedMemoryProvider(backend)

    out1 = await cached.cross_peer_context(
        observer_role="finance", query="budget", tokens=2000,
    )
    out2 = await cached.cross_peer_context(
        observer_role="finance", query="budget", tokens=2000,
    )

    # Backend invoked twice — no caching
    assert len(backend.calls) == 2
    assert out1 == "<<finance/budget>>"
    assert out2 == "<<finance/budget>>"

    # Cache untouched
    assert cached._cache == {}
