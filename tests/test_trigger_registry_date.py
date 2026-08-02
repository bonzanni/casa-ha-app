"""#396 — point-in-time (date) triggers and one-shot self-removal."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _make_bus():
    bus = MagicMock()
    bus.send = AsyncMock()
    return bus


def _make_scheduler():
    sched = MagicMock()
    sched.add_job = MagicMock()
    sched.remove_job = MagicMock()
    return sched


def _registry(scheduler=None, bus=None, on_one_shot_fired=None):
    from trigger_registry import TriggerRegistry
    return TriggerRegistry(
        scheduler=scheduler or _make_scheduler(),
        app=web.Application(),
        bus=bus or _make_bus(),
        on_one_shot_fired=on_one_shot_fired,
    )


def _future(days=1):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _past(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _date_spec(at, name="reminder-a1b2c3", one_shot=True):
    from config import TriggerSpec
    return TriggerSpec(name=name, type="date", at=at, one_shot=one_shot,
                       channel="telegram", prompt='Send this: "Bins."')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_future_date_trigger_registers_as_a_date_job():
    sched = _make_scheduler()
    reg = _registry(scheduler=sched)
    reg.register_agent("assistant", [_date_spec(_future())], ["telegram"])

    assert sched.add_job.call_count == 1
    kwargs = sched.add_job.call_args.kwargs
    assert kwargs["trigger"] == "date"
    assert kwargs["id"] == "assistant:reminder-a1b2c3"
    assert kwargs["run_date"].tzinfo is not None


async def test_past_date_trigger_does_not_register():
    """The sweep owns overdue reminders. Registering a past date job would
    either fire instantly at boot or vanish as a misfire — and both destroy
    the 'still present, still owed' evidence the sweep relies on."""
    sched = _make_scheduler()
    reg = _registry(scheduler=sched)
    reg.register_agent("assistant", [_date_spec(_past())], ["telegram"])

    assert sched.add_job.call_count == 0


async def test_past_date_trigger_does_not_consume_the_job_id():
    """A skipped past reminder must not poison a later re-registration of the
    same name (reload paths call register_agent again)."""
    sched = _make_scheduler()
    reg = _registry(scheduler=sched)
    reg.register_agent("assistant", [_date_spec(_past())], ["telegram"])
    reg.register_agent("assistant", [_date_spec(_future())], ["telegram"])

    assert sched.add_job.call_count == 1


async def test_date_trigger_requires_a_declared_channel():
    from trigger_registry import TriggerError
    reg = _registry()
    with pytest.raises(TriggerError):
        reg.register_agent("assistant", [_date_spec(_future())], ["voice"])


async def test_date_trigger_rejects_a_naive_at():
    reg = _registry()
    with pytest.raises(ValueError):
        reg.register_agent("assistant",
                           [_date_spec("2099-08-03T08:00:00")], ["telegram"])


async def test_date_trigger_rejects_an_empty_at():
    reg = _registry()
    with pytest.raises(ValueError):
        reg.register_agent("assistant", [_date_spec("")], ["telegram"])


# ---------------------------------------------------------------------------
# Firing and one-shot self-removal  (INV-TRIG-006)
# ---------------------------------------------------------------------------


async def _fire_the_only_job(sched):
    """Invoke the callable the registry handed to APScheduler."""
    fn = sched.add_job.call_args.args[0]
    await fn()


async def test_firing_a_one_shot_delivers_then_removes_job_and_entry():
    sched, bus, seen = _make_scheduler(), _make_bus(), []
    reg = _registry(scheduler=sched, bus=bus,
                    on_one_shot_fired=lambda r, n: seen.append((r, n)))
    reg.register_agent("assistant", [_date_spec(_future())], ["telegram"])

    await _fire_the_only_job(sched)

    bus.send.assert_awaited_once()
    sched.remove_job.assert_called_once_with("assistant:reminder-a1b2c3")
    assert seen == [("assistant", "reminder-a1b2c3")]


async def test_firing_a_one_shot_frees_its_job_id():
    sched = _make_scheduler()
    reg = _registry(scheduler=sched)
    reg.register_agent("assistant", [_date_spec(_future())], ["telegram"])
    await _fire_the_only_job(sched)

    # Re-registering the same name must not trip the duplicate-id guard.
    reg.register_agent("assistant", [_date_spec(_future())], ["telegram"])
    assert sched.add_job.call_count == 2


async def test_a_recurring_trigger_never_invokes_the_remover():
    from config import TriggerSpec
    sched, seen = _make_scheduler(), []
    reg = _registry(scheduler=sched,
                    on_one_shot_fired=lambda r, n: seen.append((r, n)))
    reg.register_agent("assistant", [TriggerSpec(
        name="reminder-rec333", type="cron", schedule="0 7 * * thu",
        one_shot=False, channel="telegram", prompt="x")], ["telegram"])

    await _fire_the_only_job(sched)

    assert seen == []
    sched.remove_job.assert_not_called()


async def test_a_failing_remover_does_not_break_delivery():
    """At-least-once (spec §8): the message is already on the bus, so a
    cleanup failure must leave the entry for the sweep to retry rather than
    raise out of the scheduled job."""
    def boom(role, name):
        raise OSError("disk full")

    sched, bus = _make_scheduler(), _make_bus()
    reg = _registry(scheduler=sched, bus=bus, on_one_shot_fired=boom)
    reg.register_agent("assistant", [_date_spec(_future())], ["telegram"])

    await _fire_the_only_job(sched)   # must not raise

    bus.send.assert_awaited_once()


async def test_one_shot_works_without_a_remover_configured():
    """Default None keeps every pre-existing construction site working."""
    sched, bus = _make_scheduler(), _make_bus()
    reg = _registry(scheduler=sched, bus=bus)
    reg.register_agent("assistant", [_date_spec(_future())], ["telegram"])

    await _fire_the_only_job(sched)

    bus.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# remove_job_for  (used by cancel_reminder)
# ---------------------------------------------------------------------------


async def test_remove_job_for_drops_a_live_job():
    sched = _make_scheduler()
    reg = _registry(scheduler=sched)
    reg.register_agent("assistant", [_date_spec(_future())], ["telegram"])

    assert reg.remove_job_for("assistant", "reminder-a1b2c3") is True
    sched.remove_job.assert_called_once_with("assistant:reminder-a1b2c3")

    # Freed, so the name can be reused.
    reg.register_agent("assistant", [_date_spec(_future())], ["telegram"])
    assert sched.add_job.call_count == 2


async def test_remove_job_for_absent_job_is_false():
    sched = _make_scheduler()
    sched.remove_job.side_effect = Exception("no such job")
    reg = _registry(scheduler=sched)
    assert reg.remove_job_for("assistant", "reminder-nope00") is False
