"""H4 (v0.53.0) regression: the observer bus target must have a run_agent_loop
consumer (casa_core step 13 via _bus_loop_targets), else engagement events sent
to target='observer' enqueue forever with no consumer — lost + memory leak."""

import asyncio
from unittest.mock import MagicMock

import pytest

from bus import BusMessage, MessageBus, MessageType
from observer import Observer

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_bus_loop_targets_includes_observer():
    from casa_core import _bus_loop_targets
    assert "observer" in _bus_loop_targets({})
    # Dedup + order preserved; a resident literally named "observer" must not
    # produce two consumers.
    targets = _bus_loop_targets({"assistant": object(), "observer": object()})
    assert targets.count("observer") == 1
    assert "assistant" in targets and "telegram" in targets


async def test_observer_queue_is_drained_by_casa_core_spawn_list():
    from casa_core import _bus_loop_targets

    bus = MessageBus()
    obs = Observer(bus=bus, engagement_registry=MagicMock(), model_name="haiku")
    handled = asyncio.Event()

    async def _fake_handle(msg):
        handled.set()

    obs._handle_event = _fake_handle  # bind BEFORE subscribe
    await obs.subscribe()

    agents: dict = {}  # residents irrelevant; "telegram" not registered here
    tasks = [
        asyncio.create_task(bus.run_agent_loop(name))
        for name in _bus_loop_targets(agents)
        if name in bus.queues
    ]
    try:
        assert tasks, "no run_agent_loop consumer spawned for 'observer'"
        await bus.notify(BusMessage(
            type=MessageType.NOTIFICATION,
            source="claude_code_driver",
            target="observer",
            content={"event": "subprocess_respawn", "engagement_id": "e1"},
        ))
        await asyncio.wait_for(handled.wait(), timeout=1.0)
        await asyncio.wait_for(bus.queues["observer"].join(), timeout=1.0)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
