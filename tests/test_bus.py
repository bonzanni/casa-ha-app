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
