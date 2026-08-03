"""#396 / INV-TRIG-008 — the sweep delivers reminders missed while down."""

from __future__ import annotations

import pathlib
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
    registry: object
    reminders_path: str
    butler_reminders_path: str
    triggers_path: str


@pytest.fixture
def env(tmp_path):
    agents_dir = tmp_path / "agents"
    for role in ("assistant", "butler"):
        (agents_dir / role).mkdir(parents=True)
        _seed(agents_dir / role / "triggers.yaml")

    from aiohttp import web
    from trigger_registry import TriggerRegistry

    bus = MagicMock()
    bus.send = AsyncMock()
    registry = TriggerRegistry(scheduler=MagicMock(), app=web.Application(),
                               bus=bus)
    runtime = types.SimpleNamespace(
        agents_dir=str(agents_dir), bus=bus, trigger_registry=registry,
        role_configs={"assistant": object(), "butler": object()},
    )
    return Env(
        runtime=runtime, bus=bus, registry=registry,
        reminders_path=str(agents_dir / "assistant" / "reminders.yaml"),
        butler_reminders_path=str(agents_dir / "butler" / "reminders.yaml"),
        triggers_path=str(agents_dir / "assistant" / "triggers.yaml"))


def _reminder(name="reminder-old111", at=OVERDUE, text="Bins."):
    return {"name": name, "type": "date", "at": at, "one_shot": True,
            "channel": "telegram",
            "prompt": f'Send this exact message via telegram: "{text}"'}


def _names(path):
    with open(path, encoding="utf-8") as fh:
        return [t["name"] for t in yaml.safe_load(fh)["triggers"]]


async def test_overdue_reminder_is_delivered_and_removed(env):
    reminders.append_entry(env.reminders_path, _reminder())

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 1
    env.bus.send.assert_awaited_once()
    msg = env.bus.send.await_args.args[0]
    assert "Bins." in msg.content
    assert msg.target == "assistant"
    assert msg.channel == "telegram"
    assert "reminder-old111" not in _names(env.reminders_path)


async def test_the_swept_message_is_marked_late(env):
    reminders.append_entry(env.reminders_path, _reminder())
    await reminders.sweep_reminders(env.runtime, NOW)
    msg = env.bus.send.await_args.args[0]
    assert msg.context.get("late") is True
    assert msg.context.get("trigger") == "reminder-old111"


async def test_future_reminder_is_left_alone(env):
    reminders.append_entry(env.reminders_path, _reminder("reminder-new222", LATER))

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 0
    env.bus.send.assert_not_awaited()
    assert "reminder-new222" in _names(env.reminders_path)


async def test_recurring_reminders_are_never_swept(env):
    reminders.append_entry(env.reminders_path, {
        "name": "reminder-rec333", "type": "cron", "schedule": "0 7 * * thu",
        "one_shot": False, "channel": "telegram", "prompt": "x"})

    assert await reminders.sweep_reminders(env.runtime, NOW) == 0
    env.bus.send.assert_not_awaited()
    assert "reminder-rec333" in _names(env.reminders_path)


async def test_operator_triggers_are_never_swept(env):
    """The heartbeat and morning briefing live in triggers.yaml and are not
    the sweep's business — it only ever reads reminders.yaml."""
    assert await reminders.sweep_reminders(env.runtime, NOW) == 0
    env.bus.send.assert_not_awaited()
    assert _names(env.triggers_path) == ["heartbeat"]


async def test_the_sweep_never_touches_the_operator_file(env):
    """config_sync can overwrite triggers.yaml on an update; reminders must
    not be in it. Prove the sweep neither reads nor writes it."""
    before = pathlib.Path(env.triggers_path).read_text()
    reminders.append_entry(env.reminders_path, _reminder())

    await reminders.sweep_reminders(env.runtime, NOW)

    assert pathlib.Path(env.triggers_path).read_text() == before


async def test_every_role_is_swept_not_just_the_first(env):
    reminders.append_entry(env.reminders_path, _reminder("reminder-aaa111"))
    reminders.append_entry(env.butler_reminders_path, _reminder("reminder-bbb222"))

    assert await reminders.sweep_reminders(env.runtime, NOW) == 2

    targets = {c.args[0].target for c in env.bus.send.await_args_list}
    assert targets == {"assistant", "butler"}


async def test_removal_failure_leaves_it_for_the_next_sweep(env, monkeypatch):
    """At-least-once (spec §8): a duplicate nudge beats a missed reminder."""
    reminders.append_entry(env.reminders_path, _reminder())

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(reminders, "remove_entry", boom)
    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 1
    env.bus.send.assert_awaited_once()
    assert "reminder-old111" in _names(env.reminders_path)


async def test_a_delivery_failure_does_not_remove_the_entry(env):
    """If the bus rejects the send, the reminder is still owed."""
    reminders.append_entry(env.reminders_path, _reminder())
    env.bus.send.side_effect = RuntimeError("bus down")

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 0
    assert "reminder-old111" in _names(env.reminders_path)


async def test_one_bad_role_does_not_stop_the_others(env):
    reminders.append_entry(env.butler_reminders_path, _reminder("reminder-bbb222"))
    env.runtime.role_configs = {"ghost": object(), "butler": object()}

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1


async def test_sweeping_twice_delivers_once(env):
    reminders.append_entry(env.reminders_path, _reminder())

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1
    assert await reminders.sweep_reminders(env.runtime, NOW) == 0


# ---------------------------------------------------------------------------
# Exclusive ownership between the scheduler and the sweep
# ---------------------------------------------------------------------------


async def test_a_reminder_with_a_live_job_is_not_swept(env):
    """The scheduler still owns it and WILL deliver it. Without this the two
    race for a reminder whose time has just passed and it arrives twice."""
    from config import TriggerSpec

    reminders.append_entry(env.reminders_path, _reminder())
    # Register a live job under the same role:name.
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-old111", type="cron", schedule="0 7 * * thu",
        channel="telegram", prompt="x")], ["telegram"])

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 0
    env.bus.send.assert_not_awaited()
    assert "reminder-old111" in _names(env.reminders_path)


async def test_an_overdue_reminder_without_a_live_job_is_swept(env):
    """The post-restart case: jobs are memory-only, so a past-dated reminder
    has no job and the sweep is the only thing that can deliver it."""
    reminders.append_entry(env.reminders_path, _reminder())

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1


async def test_an_entry_with_no_prompt_is_left_in_place(env):
    """Refuse rather than send an empty message and delete the evidence."""
    bad = _reminder()
    bad["prompt"] = "   "
    reminders.append_entry(env.reminders_path, bad)

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 0
    env.bus.send.assert_not_awaited()
    assert "reminder-old111" in _names(env.reminders_path)


async def test_a_date_entry_missing_one_shot_is_still_swept(env):
    """Defence in depth: membership is decided on type alone, so an entry
    that somehow lacked the flag is not skipped by BOTH registration and the
    sweep and thereby lost."""
    entry = _reminder()
    del entry["one_shot"]
    reminders.append_entry(env.reminders_path, entry)

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1


# ---------------------------------------------------------------------------
# The sweep as a convergence loop — the store is the truth (Sol r3 #1)
# ---------------------------------------------------------------------------


async def test_a_recurring_reminder_without_a_job_is_reregistered(env):
    """A reload that re-registers a role from a snapshot taken before the
    reminder was written drops its job. Only one-shots are recoverable by
    delivery, so without this a recurring reminder would never fire again
    until the next restart."""
    env.runtime.role_configs = {
        "assistant": types.SimpleNamespace(channels=["telegram"]),
    }
    reminders.append_entry(env.reminders_path, {
        "name": "reminder-rec333", "type": "cron", "schedule": "0 7 * * thu",
        "at": "2099-08-06T07:00:00+02:00", "one_shot": False,
        "channel": "telegram", "prompt": "Gym."})

    await reminders.sweep_reminders(env.runtime, NOW)

    assert env.registry.has_job("assistant", "reminder-rec333")


async def test_a_future_one_shot_without_a_job_is_reregistered(env):
    env.runtime.role_configs = {
        "assistant": types.SimpleNamespace(channels=["telegram"]),
    }
    reminders.append_entry(env.reminders_path,
                           _reminder("reminder-fut444", at=LATER))

    await reminders.sweep_reminders(env.runtime, NOW)

    assert env.registry.has_job("assistant", "reminder-fut444")


async def test_a_past_one_shot_is_delivered_not_reregistered(env):
    env.runtime.role_configs = {
        "assistant": types.SimpleNamespace(channels=["telegram"]),
    }
    reminders.append_entry(env.reminders_path, _reminder())

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1
    assert not env.registry.has_job("assistant", "reminder-old111")


async def test_reconciliation_is_idempotent(env):
    env.runtime.role_configs = {
        "assistant": types.SimpleNamespace(channels=["telegram"]),
    }
    reminders.append_entry(env.reminders_path, {
        "name": "reminder-rec333", "type": "cron", "schedule": "0 7 * * thu",
        "at": "2099-08-06T07:00:00+02:00", "one_shot": False,
        "channel": "telegram", "prompt": "Gym."})

    await reminders.sweep_reminders(env.runtime, NOW)
    await reminders.sweep_reminders(env.runtime, NOW)   # must not raise

    assert env.registry.has_job("assistant", "reminder-rec333")


async def test_an_unregisterable_entry_does_not_stop_the_others(env):
    """An entry naming a channel the role does not declare must not abort
    reconciliation for the rest."""
    env.runtime.role_configs = {
        "assistant": types.SimpleNamespace(channels=["telegram"]),
    }
    reminders.append_entry(env.reminders_path, {
        "name": "reminder-bad555", "type": "cron", "schedule": "0 7 * * thu",
        "one_shot": False, "channel": "voice", "prompt": "x"})
    reminders.append_entry(env.reminders_path, {
        "name": "reminder-ok6666", "type": "cron", "schedule": "0 7 * * thu",
        "one_shot": False, "channel": "telegram", "prompt": "x"})

    await reminders.sweep_reminders(env.runtime, NOW)

    assert not env.registry.has_job("assistant", "reminder-bad555")
    assert env.registry.has_job("assistant", "reminder-ok6666")


async def test_a_job_with_no_entry_left_is_dropped(env):
    """Sol r4 #2: a cancellation that raced a reload — which re-registers the
    role from a snapshot taken before the cancellation — would otherwise leave
    the reminder firing forever despite cancel_reminder reporting success."""
    from config import TriggerSpec

    env.runtime.role_configs = {
        "assistant": types.SimpleNamespace(channels=["telegram"]),
    }
    # A live job whose entry is NOT in the store (the stale-reload case).
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-ghost01", type="cron", schedule="0 7 * * thu",
        channel="telegram", prompt="x", from_reminder_store=True)],
        ["telegram"])
    assert env.registry.has_job("assistant", "reminder-ghost01")

    await reminders.sweep_reminders(env.runtime, NOW)

    assert not env.registry.has_job("assistant", "reminder-ghost01")


async def test_operator_triggers_are_never_dropped_by_reconciliation(env):
    """Only reminder-prefixed jobs are the sweep's to remove."""
    from config import TriggerSpec

    env.runtime.role_configs = {
        "assistant": types.SimpleNamespace(channels=["telegram"]),
    }
    env.registry.register_agent("assistant", [TriggerSpec(
        name="heartbeat", type="interval", minutes=60,
        channel="telegram", prompt="hb")], ["telegram"])

    await reminders.sweep_reminders(env.runtime, NOW)

    assert env.registry.has_job("assistant", "heartbeat")


async def test_an_operator_authored_reminder_prefixed_trigger_survives(env):
    """Round 5 (both reviewers): the schema REQUIRES a date trigger to carry
    the reminder prefix, so an operator may legitimately author one in their
    own triggers.yaml. Dropping its job because it is absent from
    reminders.yaml would stop it firing entirely. Provenance, not the name
    pattern, decides ownership."""
    from config import TriggerSpec

    env.runtime.role_configs = {
        "assistant": types.SimpleNamespace(channels=["telegram"]),
    }
    # Operator declares it in THEIR file...
    with open(env.triggers_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"schema_version": 1, "triggers": [
            {"name": "heartbeat", "type": "interval", "minutes": 60,
             "channel": "telegram", "prompt": "hb"},
            {"name": "reminder-maintenance", "type": "cron",
             "schedule": "0 3 * * sun", "channel": "telegram",
             "prompt": "maintenance"}]}, fh, sort_keys=False)
    # ...and it is registered, but has no reminders.yaml entry.
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-maintenance", type="cron", schedule="0 3 * * sun",
        channel="telegram", prompt="maintenance")], ["telegram"])

    await reminders.sweep_reminders(env.runtime, NOW)

    assert env.registry.has_job("assistant", "reminder-maintenance")


async def test_a_genuine_orphan_is_still_dropped(env):
    """The operator exemption must not disable reverse reconciliation."""
    from config import TriggerSpec

    env.runtime.role_configs = {
        "assistant": types.SimpleNamespace(channels=["telegram"]),
    }
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-ghost02", type="cron", schedule="0 7 * * thu",
        channel="telegram", prompt="x", from_reminder_store=True)],
        ["telegram"])

    await reminders.sweep_reminders(env.runtime, NOW)

    assert not env.registry.has_job("assistant", "reminder-ghost02")




async def test_provenance_not_the_name_decides_what_may_be_dropped(env):
    """Round 7 (both reviewers, third round on this one function): the schema
    REQUIRES every date trigger to carry the reminder prefix, so an operator
    may legitimately author one. Inferring provenance by re-reading their file
    found a new deletion bug every round; it is now carried as DATA on the
    spec, so a name alone can never authorise removal."""
    from config import TriggerSpec

    env.runtime.role_configs = {
        "assistant": types.SimpleNamespace(channels=["telegram"]),
    }
    # Same name shape, opposite provenance.
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-operator", type="cron", schedule="0 3 * * sun",
        channel="telegram", prompt="operator")], ["telegram"])
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-orphaned", type="cron", schedule="0 3 * * sun",
        channel="telegram", prompt="ours", from_reminder_store=True)],
        ["telegram"])

    await reminders.sweep_reminders(env.runtime, NOW)

    assert env.registry.has_job("assistant", "reminder-operator")
    assert not env.registry.has_job("assistant", "reminder-orphaned")


async def test_a_malformed_operator_file_is_now_irrelevant(env):
    """The sweep no longer reads triggers.yaml at all, so nothing about its
    contents can affect reconciliation or abort the pass."""
    env.runtime.role_configs = {
        "assistant": types.SimpleNamespace(channels=["telegram"]),
        "butler": types.SimpleNamespace(channels=["telegram"]),
    }
    with open(env.triggers_path, "w", encoding="utf-8") as fh:
        fh.write("{{{ not: valid: yaml\n")
    reminders.append_entry(env.butler_reminders_path,
                           _reminder("reminder-b0b0b0b0"))

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1
