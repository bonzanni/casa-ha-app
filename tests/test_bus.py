"""Tests for bus.py -- async message bus."""

import asyncio

import pytest

from bus import BusMessage, MessageBus, MessageType

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _msg(source: str = "a", target: str = "b", content: str = "hi", priority: int = 1):
    return BusMessage(
        type=MessageType.NOTIFICATION,
        source=source,
        target=target,
        content=content,
        priority=priority,
    )


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


async def test_register_and_send():
    """Handler receives the sent message."""
    bus = MessageBus()
    received = []

    async def handler(msg: BusMessage):
        received.append(msg)
        return None

    bus.register("b", handler)
    await bus.send(_msg())

    # Drain one item from the queue via run_agent_loop (run briefly)
    task = asyncio.create_task(bus.run_agent_loop("b"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(received) == 1
    assert received[0].content == "hi"


async def test_send_to_unknown_target():
    """Sending to an unregistered target does not crash."""
    bus = MessageBus()
    bus.register("a")
    await bus.send(_msg(target="nonexistent"))
    # No exception -- pass


async def test_request_response():
    """request() resolves when respond() is called."""
    bus = MessageBus()

    async def echo_handler(msg: BusMessage):
        resp = BusMessage(
            type=MessageType.RESPONSE,
            source="b",
            target="a",
            content=f"echo:{msg.content}",
        )
        return resp

    bus.register("a")
    bus.register("b", echo_handler)

    # Start agent loop for b in background
    loop_task = asyncio.create_task(bus.run_agent_loop("b"))

    req = _msg(source="a", target="b", content="ping")
    result = await bus.request(req, timeout=2)

    assert result.content == "echo:ping"

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


async def test_request_timeout():
    """request() raises TimeoutError when nobody responds."""
    bus = MessageBus()

    # Handler that does NOT produce a response
    async def black_hole(msg: BusMessage):
        return None

    bus.register("a")
    bus.register("b", black_hole)
    loop_task = asyncio.create_task(bus.run_agent_loop("b"))

    req = _msg(source="a", target="b")
    with pytest.raises(asyncio.TimeoutError):
        await bus.request(req, timeout=0.1)

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


async def test_priority_ordering():
    """Lower priority number arrives first."""
    bus = MessageBus()
    received = []

    async def collector(msg: BusMessage):
        received.append(msg.priority)
        return None

    bus.register("b", collector)

    # Enqueue out of order
    await bus.send(_msg(priority=2))
    await bus.send(_msg(priority=0))
    await bus.send(_msg(priority=1))

    # Process all
    task = asyncio.create_task(bus.run_agent_loop("b"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert received == [0, 1, 2]


async def test_log_bounded():
    """Log respects max_log_size."""
    bus = MessageBus(max_log_size=5)
    bus.register("b")

    for i in range(10):
        await bus.send(_msg(content=str(i)))

    log = bus.get_log(last_n=100)
    assert len(log) == 5
    # Last 5 messages should be content 5..9
    assert [m.content for m in log] == ["5", "6", "7", "8", "9"]


async def test_get_log_slice():
    """get_log(last_n) returns only the requested slice."""
    bus = MessageBus()
    bus.register("b")

    for i in range(10):
        await bus.send(_msg(content=str(i)))

    log = bus.get_log(last_n=3)
    assert len(log) == 3
    assert [m.content for m in log] == ["7", "8", "9"]


async def test_handlers_dispatch_concurrently():
    """Two slow handlers on the same target must run in parallel.

    Regression guard for v0.2.1: run_agent_loop used to `await handler(msg)`
    inline, serialising messages on the same agent. The fix wraps each
    handler in `asyncio.create_task`. With two 200ms handlers, concurrent
    dispatch completes in ~200ms; a serialised dispatch would take ~400ms.
    """
    import time

    bus = MessageBus()
    completions: list[float] = []

    async def slow_handler(msg: BusMessage):
        await asyncio.sleep(0.2)
        completions.append(time.monotonic())
        return None

    bus.register("b", slow_handler)

    loop_task = asyncio.create_task(bus.run_agent_loop("b"))

    start = time.monotonic()
    await bus.send(_msg())
    await bus.send(_msg())

    # Wait for both handlers to complete.
    for _ in range(50):
        if len(completions) >= 2:
            break
        await asyncio.sleep(0.02)

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task

    assert len(completions) == 2
    elapsed = completions[-1] - start
    # Serial would be ~0.4s; concurrent should be well under 0.35s.
    assert elapsed < 0.35, f"handlers appear serialised ({elapsed:.3f}s elapsed)"


async def test_handler_exception_unblocks_request_caller():
    """A handler raising must resolve the pending REQUEST with an error
    response, not leave the caller hanging until the 300s timeout."""
    bus = MessageBus()

    async def explodes(msg: BusMessage):
        raise RuntimeError("boom")

    bus.register("a")
    bus.register("b", explodes)
    loop_task = asyncio.create_task(bus.run_agent_loop("b"))

    req = _msg(source="a", target="b", content="ping")
    # If the fix is regressed this hangs until bus.request's 300s timeout.
    # With the fix, respond() is called from _dispatch's except block.
    result = await bus.request(req, timeout=2)
    assert result.type == MessageType.RESPONSE
    assert "handler error" in str(result.content)

    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


# ---------------------------------------------------------------------------
# Correlation-id propagation (spec 5.2 §7)
# ---------------------------------------------------------------------------


class TestDispatchCid:
    async def test_dispatcher_sets_cid_from_context(self):
        from log_cid import cid_var

        bus = MessageBus()
        seen: list[str] = []

        async def handler(msg: BusMessage):
            seen.append(cid_var.get())
            return None

        bus.register("b", handler)
        msg = BusMessage(
            type=MessageType.NOTIFICATION, source="a", target="b",
            content="hi", context={"cid": "cafe0001"},
        )
        await bus.send(msg)

        loop = asyncio.create_task(bus.run_agent_loop("b"))
        try:
            await asyncio.sleep(0.05)
        finally:
            loop.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop

        assert seen == ["cafe0001"]

    async def test_dispatcher_uses_dash_when_context_missing(self):
        from log_cid import cid_var

        bus = MessageBus()
        seen: list[str] = []

        async def handler(msg: BusMessage):
            seen.append(cid_var.get())
            return None

        bus.register("b", handler)
        # Message without a cid in its context (backward-compat path).
        msg = BusMessage(
            type=MessageType.NOTIFICATION, source="a", target="b",
            content="hi",  # context defaults to {}
        )
        await bus.send(msg)

        loop = asyncio.create_task(bus.run_agent_loop("b"))
        try:
            await asyncio.sleep(0.05)
        finally:
            loop.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop

        assert seen == ["-"]

    async def test_concurrent_dispatches_observe_distinct_cids(self):
        """Two messages dispatched in parallel — each handler observes
        its own cid because each _dispatch runs in its own task."""
        from log_cid import cid_var

        bus = MessageBus()
        gate = asyncio.Event()
        barrier: list[str] = []

        async def handler(msg: BusMessage):
            # Capture cid, then wait until both handlers are running
            # to force true concurrency. Capture again post-wait to
            # prove no cross-contamination across tasks.
            first = cid_var.get()
            barrier.append(first)
            if len(barrier) < 2:
                while len(barrier) < 2:
                    await asyncio.sleep(0.005)
            else:
                gate.set()
            await gate.wait()
            second = cid_var.get()
            barrier.append(second)
            return None

        bus.register("b", handler)
        await bus.send(BusMessage(
            type=MessageType.NOTIFICATION, source="a", target="b",
            content="one", context={"cid": "11111111"},
        ))
        await bus.send(BusMessage(
            type=MessageType.NOTIFICATION, source="a", target="b",
            content="two", context={"cid": "22222222"},
        ))

        loop = asyncio.create_task(bus.run_agent_loop("b"))
        try:
            await asyncio.wait_for(gate.wait(), timeout=1.0)
            # Give both handlers time to resume and capture second cid.
            await asyncio.sleep(0.1)
        finally:
            loop.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop

        # barrier has 4 entries: [cid_A_before, cid_B_before,
        # cid_A_after, cid_B_after] (order of pairs may vary).
        # The two distinct cids each appear exactly twice.
        assert sorted(barrier) == ["11111111", "11111111", "22222222", "22222222"]

    async def test_dispatch_does_not_leak_cid_into_outer_context(self):
        """After _dispatch completes, cid_var in the run_agent_loop task
        must still read its prior value (reset via token)."""
        from log_cid import cid_var

        bus = MessageBus()

        async def handler(msg: BusMessage):
            return None

        bus.register("b", handler)
        await bus.send(BusMessage(
            type=MessageType.NOTIFICATION, source="a", target="b",
            content="hi", context={"cid": "feedface"},
        ))

        # Observe cid_var from a captured run_agent_loop coroutine.
        async def observer():
            try:
                while True:
                    await asyncio.sleep(0.02)
                    # The outer (observer) task's cid_var must always be
                    # the default — it never ran inside _dispatch.
                    assert cid_var.get() == "-"
            except asyncio.CancelledError:
                raise

        loop = asyncio.create_task(bus.run_agent_loop("b"))
        obs = asyncio.create_task(observer())
        try:
            await asyncio.sleep(0.1)
        finally:
            loop.cancel()
            obs.cancel()
            for t in (loop, obs):
                with pytest.raises(asyncio.CancelledError):
                    await t


class TestRegisterIdempotent:
    """Granular reload (v0.35.1) — re-registering an existing role must
    rebind the handler WITHOUT replacing the queue. Replacing the queue
    orphans the running ``run_agent_loop`` task on the old queue while
    new ``send()`` calls land on the new queue, hanging every turn.

    Bug surfaced in v0.35.0 live verify (2026-05-02): after
    ``casactl reload --scope=agent --role=assistant``,
    ``POST /invoke/assistant`` hung until 504 because the dispatch loop
    was reading from a queue no producer wrote to anymore.
    """

    async def test_reregister_preserves_queue_and_rebinds_handler(self):
        bus = MessageBus()
        first_calls: list[str] = []
        second_calls: list[str] = []

        async def first(msg):
            first_calls.append(msg.content)

        async def second(msg):
            second_calls.append(msg.content)

        bus.register("b", first)
        original_queue = bus.queues["b"]

        bus.register("b", second)
        # Queue identity preserved → existing run_agent_loop tasks see
        # subsequent sends.
        assert bus.queues["b"] is original_queue
        # Handler rebound → next dispatch hits the new function.
        assert bus.handlers["b"] is second

    async def test_reregister_does_not_orphan_dispatch_loop(self):
        """End-to-end: queue/loop survives a re-register and the new
        handler receives messages sent after the rebind."""
        bus = MessageBus()
        old_calls: list[str] = []
        new_calls: list[str] = []

        async def old(msg):
            old_calls.append(msg.content)

        async def new(msg):
            new_calls.append(msg.content)

        bus.register("b", old)
        loop_task = asyncio.create_task(bus.run_agent_loop("b"))
        try:
            await bus.send(_msg(content="pre-rebind"))
            await asyncio.sleep(0.05)
            assert old_calls == ["pre-rebind"]

            # Simulate reload_agent's atomic-swap step.
            bus.register("b", new)
            await bus.send(_msg(content="post-rebind"))
            await asyncio.sleep(0.05)

            # Old handler stayed at one call; new one received the
            # second message via the same queue.
            assert old_calls == ["pre-rebind"]
            assert new_calls == ["post-rebind"]
        finally:
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task
