"""#396 / INV-TRIG-008 — the sweep delivers reminders missed while down."""

from __future__ import annotations

import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import reminders

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

CEST = timezone(timedelta(hours=2))
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=CEST)

OVERDUE = "2026-08-03T08:00:00+02:00"
LATER = "2026-08-03T20:00:00+02:00"


def _seed(path):
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"schema_version": 1, "triggers": [
            {"name": "heartbeat", "type": "interval", "minutes": 60,
             "channel": "telegram", "prompt": "hb"}]}, fh, sort_keys=False)


@dataclass
class Env:
    runtime: object
    bus: object
    triggers_path: str
    butler_triggers_path: str


@pytest.fixture
def env(tmp_path):
    agents_dir = tmp_path / "agents"
    for role in ("assistant", "butler"):
        (agents_dir / role).mkdir(parents=True)
        _seed(agents_dir / role / "triggers.yaml")

    bus = MagicMock()
    bus.send = AsyncMock()
    runtime = types.SimpleNamespace(
        agents_dir=str(agents_dir), bus=bus,
        role_configs={"assistant": object(), "butler": object()},
    )
    return Env(runtime=runtime, bus=bus,
               triggers_path=str(agents_dir / "assistant" / "triggers.yaml"),
               butler_triggers_path=str(agents_dir / "butler" / "triggers.yaml"))


def _reminder(name="reminder-old111", at=OVERDUE, text="Bins."):
    return {"name": name, "type": "date", "at": at, "one_shot": True,
            "channel": "telegram",
            "prompt": f'Send this exact message via telegram: "{text}"'}


def _names(path):
    with open(path, encoding="utf-8") as fh:
        return [t["name"] for t in yaml.safe_load(fh)["triggers"]]


async def test_overdue_reminder_is_delivered_and_removed(env):
    reminders.append_entry(env.triggers_path, _reminder())

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 1
    env.bus.send.assert_awaited_once()
    msg = env.bus.send.await_args.args[0]
    assert "Bins." in msg.content
    assert msg.target == "assistant"
    assert msg.channel == "telegram"
    assert "reminder-old111" not in _names(env.triggers_path)


async def test_the_swept_message_is_marked_late(env):
    reminders.append_entry(env.triggers_path, _reminder())
    await reminders.sweep_reminders(env.runtime, NOW)
    msg = env.bus.send.await_args.args[0]
    assert msg.context.get("late") is True
    assert msg.context.get("trigger") == "reminder-old111"


async def test_future_reminder_is_left_alone(env):
    reminders.append_entry(env.triggers_path, _reminder("reminder-new222", LATER))

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 0
    env.bus.send.assert_not_awaited()
    assert "reminder-new222" in _names(env.triggers_path)


async def test_recurring_reminders_are_never_swept(env):
    reminders.append_entry(env.triggers_path, {
        "name": "reminder-rec333", "type": "cron", "schedule": "0 7 * * thu",
        "one_shot": False, "channel": "telegram", "prompt": "x"})

    assert await reminders.sweep_reminders(env.runtime, NOW) == 0
    env.bus.send.assert_not_awaited()
    assert "reminder-rec333" in _names(env.triggers_path)


async def test_operator_triggers_are_never_swept(env):
    """The heartbeat and morning briefing are not the sweep's business."""
    assert await reminders.sweep_reminders(env.runtime, NOW) == 0
    env.bus.send.assert_not_awaited()
    assert _names(env.triggers_path) == ["heartbeat"]


async def test_every_role_is_swept_not_just_the_first(env):
    reminders.append_entry(env.triggers_path, _reminder("reminder-aaa111"))
    reminders.append_entry(env.butler_triggers_path, _reminder("reminder-bbb222"))

    assert await reminders.sweep_reminders(env.runtime, NOW) == 2

    targets = {c.args[0].target for c in env.bus.send.await_args_list}
    assert targets == {"assistant", "butler"}


async def test_removal_failure_leaves_it_for_the_next_sweep(env, monkeypatch):
    """At-least-once (spec §8): a duplicate nudge beats a missed reminder."""
    reminders.append_entry(env.triggers_path, _reminder())

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(reminders, "remove_entry", boom)
    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 1
    env.bus.send.assert_awaited_once()
    assert "reminder-old111" in _names(env.triggers_path)


async def test_a_delivery_failure_does_not_remove_the_entry(env):
    """If the bus rejects the send, the reminder is still owed."""
    reminders.append_entry(env.triggers_path, _reminder())
    env.bus.send.side_effect = RuntimeError("bus down")

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 0
    assert "reminder-old111" in _names(env.triggers_path)


async def test_one_bad_role_does_not_stop_the_others(env):
    reminders.append_entry(env.butler_triggers_path, _reminder("reminder-bbb222"))
    env.runtime.role_configs = {"ghost": object(), "butler": object()}

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1


async def test_sweeping_twice_delivers_once(env):
    reminders.append_entry(env.triggers_path, _reminder())

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1
    assert await reminders.sweep_reminders(env.runtime, NOW) == 0
