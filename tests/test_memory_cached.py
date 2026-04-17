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
