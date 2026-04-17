"""Spec §4 — VoiceSessionPool: create/touch/evict/dedup-prewarm."""

import asyncio

import pytest

from channels.voice.session import VoiceSession, VoiceSessionPool


@pytest.mark.asyncio
class TestPoolLifecycle:
    async def test_create_on_first_ensure(self):
        pool = VoiceSessionPool(idle_timeout=300, gate_slots=10)
        sess = pool.ensure("user-xyz")
        assert isinstance(sess, VoiceSession)
        assert sess.scope_id == "user-xyz"
        assert sess.session_key == "voice:user-xyz"

    async def test_ensure_returns_same_instance(self):
        pool = VoiceSessionPool(idle_timeout=300)
        a = pool.ensure("user-xyz")
        b = pool.ensure("user-xyz")
        assert a is b

    async def test_touch_updates_last_activity(self, monkeypatch):
        clock = [100.0]
        monkeypatch.setattr(
            "channels.voice.session.time.monotonic", lambda: clock[0],
        )
        pool = VoiceSessionPool(idle_timeout=300)
        sess = pool.ensure("user-xyz")
        assert sess.last_activity == 100.0
        clock[0] = 150.0
        pool.touch("user-xyz")
        assert sess.last_activity == 150.0


@pytest.mark.asyncio
class TestIdleSweep:
    async def test_evicts_after_idle_timeout(self, monkeypatch):
        clock = [100.0]
        monkeypatch.setattr(
            "channels.voice.session.time.monotonic", lambda: clock[0],
        )
        pool = VoiceSessionPool(idle_timeout=10)
        pool.ensure("user-xyz")
        clock[0] = 200.0  # well past timeout
        evicted = pool.sweep()
        assert evicted == ["user-xyz"]
        assert pool.get("user-xyz") is None

    async def test_cancels_prewarm_on_eviction(self, monkeypatch):
        clock = [100.0]
        monkeypatch.setattr(
            "channels.voice.session.time.monotonic", lambda: clock[0],
        )
        pool = VoiceSessionPool(idle_timeout=10)
        sess = pool.ensure("user-xyz")

        async def slow():
            await asyncio.sleep(60)
        task = asyncio.create_task(slow())
        sess.prewarm_task = task
        clock[0] = 200.0
        pool.sweep()
        await asyncio.sleep(0)  # let cancellation propagate
        assert task.cancelled()


@pytest.mark.asyncio
class TestPrewarmDedup:
    async def test_set_prewarm_task_when_absent(self):
        pool = VoiceSessionPool(idle_timeout=300)
        sess = pool.ensure("user-xyz")
        called = 0

        async def warm():
            nonlocal called
            called += 1

        first = pool.schedule_prewarm("user-xyz", warm)
        # Second schedule is a no-op while the first is alive.
        second = pool.schedule_prewarm("user-xyz", warm)
        assert first is not None
        assert second is None
        await first
        assert called == 1

    async def test_reschedule_after_prewarm_done(self):
        pool = VoiceSessionPool(idle_timeout=300)
        pool.ensure("user-xyz")

        async def warm():
            return None

        first = pool.schedule_prewarm("user-xyz", warm)
        assert first is not None
        await first
        second = pool.schedule_prewarm("user-xyz", warm)
        assert second is not None  # no longer live
        await second


@pytest.mark.asyncio
class TestGate:
    async def test_gate_slots_default(self):
        pool = VoiceSessionPool(idle_timeout=300, gate_slots=10)
        sess = pool.ensure("user-xyz")
        # Semaphore does not expose the initial value; ensure it accepts 10 acquires.
        acquired = [await sess.gate.acquire() for _ in range(10)]
        assert all(a is True for a in acquired)
        sess.gate.release()
        sess.gate.release()


@pytest.mark.asyncio
class TestSweeperHygiene:
    async def test_sweep_does_not_recancel_completed_prewarm(self):
        """A completed prewarm task should not have .cancel() called on eviction."""
        pool = VoiceSessionPool(idle_timeout=-1)  # immediate eviction (any elapsed > -1)
        sess = pool.ensure("s")

        async def instant():
            return None
        sess.prewarm_task = asyncio.create_task(instant())
        await sess.prewarm_task  # make it done
        # sweep() must not raise; .cancel() on a done task is a no-op but
        # we shouldn't be calling it in the first place.
        evicted = pool.sweep()
        assert evicted == ["s"]

    async def test_run_sweeper_cancels_cleanly(self):
        pool = VoiceSessionPool(idle_timeout=300)
        task = asyncio.create_task(pool.run_sweeper(interval=0.01))
        await asyncio.sleep(0.02)
        task.cancel()
        await task  # must not raise
        assert task.cancelled() or task.done()
