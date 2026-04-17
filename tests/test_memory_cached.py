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
        self._queue: list[str] = []
        self._error_on_get: Exception | None = None

    def queue(self, *responses: str) -> None:
        self._queue.extend(responses)

    async def ensure_session(self, session_id, agent_role, user_peer="nicola"):
        self.ensure_calls += 1

    async def get_context(
        self, session_id, agent_role, tokens,
        search_query=None, user_peer="nicola",
    ):
        if self._error_on_get is not None:
            raise self._error_on_get
        self.get_calls += 1
        if self._queue:
            return self._queue.pop(0)
        return f"ctx#{self.get_calls}"

    async def add_turn(
        self, session_id, agent_role, user_text, assistant_text,
        user_peer="nicola",
    ):
        self.add_calls += 1


async def _drain():
    """Let queued background tasks run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_first_call_fetches_subsequent_calls_cached():
    backend = RecordingProvider()
    backend.queue("first")
    cached = CachedMemoryProvider(backend)

    out1 = await cached.get_context("s", "assistant", 100)
    out2 = await cached.get_context("s", "assistant", 100)

    assert out1 == "first"
    assert out2 == "first"
    assert backend.get_calls == 1


async def test_search_query_does_not_participate_in_cache_key():
    backend = RecordingProvider()
    backend.queue("first")
    cached = CachedMemoryProvider(backend)

    await cached.get_context("s", "assistant", 100, search_query="a")
    await cached.get_context("s", "assistant", 100, search_query="b")

    assert backend.get_calls == 1


async def test_distinct_keys_fetch_separately():
    backend = RecordingProvider()
    backend.queue("a", "b", "c")
    cached = CachedMemoryProvider(backend)

    await cached.get_context("s1", "assistant", 100)
    await cached.get_context("s2", "assistant", 100)
    await cached.get_context("s1", "butler", 100)

    assert backend.get_calls == 3


async def test_add_turn_triggers_background_refresh():
    backend = RecordingProvider()
    backend.queue("v1", "v2")
    cached = CachedMemoryProvider(backend)

    await cached.get_context("s", "assistant", 100)
    await cached.add_turn("s", "assistant", "u", "a")
    await _drain()

    out = await cached.get_context("s", "assistant", 100)
    assert out == "v2"
    assert backend.get_calls == 2


async def test_refresh_error_does_not_crash_and_keeps_stale_cache(caplog):
    import logging

    backend = RecordingProvider()
    backend.queue("v1")
    cached = CachedMemoryProvider(backend)

    await cached.get_context("s", "assistant", 100)
    backend._error_on_get = RuntimeError("boom")

    with caplog.at_level(logging.WARNING):
        await cached.add_turn("s", "assistant", "u", "a")
        await _drain()

    # stale cache preserved, error logged
    out = await cached.get_context("s", "assistant", 100)
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
    await cached.get_context("s", "assistant", 100)

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
            *(cached.get_context("s", "assistant", 100) for _ in range(10)),
        )

        assert backend.get_calls == 1
        assert all(r == "shared" for r in results)
        assert cached._cache[("s", "assistant", 100)] == "shared"

    async def test_concurrent_cold_reads_distinct_keys_parallelize(self):
        """Each backend call sleeps 100 ms; 5 distinct keys in ~100 ms, not ~500."""
        import time

        class SlowProvider(RecordingProvider):
            async def get_context(
                self, session_id, agent_role, tokens,
                search_query=None, user_peer="nicola",
            ):
                await asyncio.sleep(0.1)
                return await super().get_context(
                    session_id, agent_role, tokens,
                    search_query=search_query, user_peer=user_peer,
                )

        backend = SlowProvider()
        backend.queue("a", "b", "c", "d", "e")
        cached = CachedMemoryProvider(backend)

        t0 = time.perf_counter()
        await asyncio.gather(
            cached.get_context("s1", "assistant", 100),
            cached.get_context("s2", "assistant", 100),
            cached.get_context("s3", "assistant", 100),
            cached.get_context("s4", "assistant", 100),
            cached.get_context("s5", "assistant", 100),
        )
        elapsed = time.perf_counter() - t0

        assert backend.get_calls == 5
        assert elapsed < 0.3, f"expected ~0.1s, got {elapsed:.2f}s"

    async def test_second_caller_awaits_in_flight_backend_call(self):
        """Second concurrent cold reader blocks on lock; no second backend call."""
        gate = asyncio.Event()
        started = asyncio.Event()

        class GatedProvider(RecordingProvider):
            async def get_context(
                self, session_id, agent_role, tokens,
                search_query=None, user_peer="nicola",
            ):
                self.get_calls += 1
                started.set()
                await gate.wait()
                return self._queue.pop(0)

        backend = GatedProvider()
        backend.queue("only")
        cached = CachedMemoryProvider(backend)

        task_a = asyncio.create_task(cached.get_context("s", "assistant", 100))
        await started.wait()
        # First call is now inside the backend; start the second — it must
        # block on the key's lock, not launch a second backend call.
        task_b = asyncio.create_task(cached.get_context("s", "assistant", 100))
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

        await cached.get_context("s", "assistant", 100)
        await cached.get_context("s", "assistant", 100)  # reuses lock
        await cached.get_context("s", "butler", 100)
        await cached.get_context("s2", "assistant", 100)

        assert backend.get_calls == 3
        assert len(cached._locks) == 3, (
            f"expected 3 distinct lock keys, got {len(cached._locks)}: "
            f"{list(cached._locks.keys())}"
        )
