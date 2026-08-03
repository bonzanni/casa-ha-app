"""#396 / INV-TRIG-008 — the sweep delivers reminders missed while down.

#398 release 2 changed the sweep's FILE, not merely its selector. The registry
deliberately leaves a past-dated `date` trigger unregistered for this sweep to
deliver (``trigger_registry._register_scheduled``), so a sweep left pointed at
the retired ``reminders.yaml`` would find nothing and the reminder would be
silently never delivered — the exact failure #396 exists to prevent.

That makes several guarantees here newly load-bearing rather than incidental:
the sweep now reads the OPERATOR's trigger file, so "an operator entry is never
delivered, never re-registered and never removed" is no longer true by virtue of
looking at a different file. It has to be true by ownership.
"""

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
# Far future ON PURPOSE, and not merely later than NOW. `past_due` compares
# against the injected NOW, but the REGISTRY compares a date trigger against the
# real wall clock — so a "later" instant on NOW's own calendar day becomes a
# past-dated trigger, silently unregistered, once that time of day passes in
# reality. #396 used 2026-08-03T20:00+02:00 here and the re-registration test
# began failing permanently at 20:00 local on 2026-08-03.
LATER = "2099-08-03T20:00:00+02:00"

HEARTBEAT = {"name": "heartbeat", "type": "interval", "minutes": 60,
             "channel": "telegram", "prompt": "hb"}


def _seed(path, triggers=(HEARTBEAT,)):
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"schema_version": 1, "triggers": list(triggers)},
                       fh, sort_keys=False)


@dataclass
class Env:
    runtime: object
    bus: object
    registry: object
    path: str            # assistant's triggers.yaml — reminders AND operator
    butler_path: str


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
        path=str(agents_dir / "assistant" / "triggers.yaml"),
        butler_path=str(agents_dir / "butler" / "triggers.yaml"))


def _reminder(name="reminder-old11111", at=OVERDUE, text="Bins.", **over):
    entry = {"name": name, "type": "date", "at": at, "one_shot": True,
             "channel": "telegram", "managed_by": "agent",
             "prompt": f'Send this exact message via telegram: "{text}"'}
    entry.update(over)
    return entry


def _operators_lookalike(name="reminder-bins", at=OVERDUE):
    """Every mark inference used to read as agent-owned, minus ``managed_by``."""
    return {"name": name, "type": "date", "at": at, "one_shot": True,
            "channel": "telegram", "prompt": "operator's own"}


def _names(path):
    with open(path, encoding="utf-8") as fh:
        return [t["name"] for t in yaml.safe_load(fh)["triggers"]]


def _channels(*roles):
    return {r: types.SimpleNamespace(channels=["telegram"]) for r in roles}


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def test_overdue_reminder_is_delivered_and_removed(env):
    reminders.add_entry(env.path, _reminder())

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 1
    env.bus.send.assert_awaited_once()
    msg = env.bus.send.await_args.args[0]
    assert "Bins." in msg.content
    assert msg.target == "assistant"
    assert msg.channel == "telegram"
    assert "reminder-old11111" not in _names(env.path)


async def test_the_sweep_reads_the_operators_trigger_file(env):
    """Trap 1, pinned. The registry leaves a past-dated trigger for the sweep,
    so a sweep on any other path delivers nothing at all.

    Red case: point ``sweep_reminders`` at a retired ``reminders.yaml`` and this
    fails — the entry sits in ``triggers.yaml`` and is never delivered.
    """
    assert env.path.endswith("assistant/triggers.yaml")
    reminders.add_entry(env.path, _reminder())

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1


async def test_the_swept_message_is_marked_late(env):
    reminders.add_entry(env.path, _reminder())
    await reminders.sweep_reminders(env.runtime, NOW)
    msg = env.bus.send.await_args.args[0]
    assert msg.context.get("late") is True
    assert msg.context.get("trigger") == "reminder-old11111"


async def test_future_reminder_is_left_alone(env):
    reminders.add_entry(env.path, _reminder("reminder-new22222", LATER))

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 0
    env.bus.send.assert_not_awaited()
    assert "reminder-new22222" in _names(env.path)


async def test_recurring_reminders_are_never_swept(env):
    reminders.add_entry(env.path, {
        "name": "reminder-rec33333", "type": "cron", "schedule": "0 7 * * thu",
        "one_shot": False, "channel": "telegram", "prompt": "x",
        "managed_by": "agent"})

    assert await reminders.sweep_reminders(env.runtime, NOW) == 0
    env.bus.send.assert_not_awaited()
    assert "reminder-rec33333" in _names(env.path)


async def test_every_role_is_swept_not_just_the_first(env):
    reminders.add_entry(env.path, _reminder("reminder-aaa11111"))
    reminders.add_entry(env.butler_path, _reminder("reminder-bbb22222"))

    assert await reminders.sweep_reminders(env.runtime, NOW) == 2

    targets = {c.args[0].target for c in env.bus.send.await_args_list}
    assert targets == {"assistant", "butler"}


async def test_removal_failure_leaves_it_for_the_next_sweep(env, monkeypatch):
    """At-least-once (spec §8): a duplicate nudge beats a missed reminder."""
    reminders.add_entry(env.path, _reminder())

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(reminders, "remove_entry", boom)
    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 1
    env.bus.send.assert_awaited_once()
    assert "reminder-old11111" in _names(env.path)


async def test_a_non_removed_outcome_also_leaves_it_queued(env, monkeypatch):
    """``remove_entry`` now reports an outcome instead of raising for the
    not-found / not-owned cases, so the sweep must notice a non-``"removed"``
    string too — otherwise a silent no-op reads as a successful cleanup."""
    reminders.add_entry(env.path, _reminder())
    monkeypatch.setattr(reminders, "remove_entry", lambda *a, **k: "not_owned")

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1
    assert "reminder-old11111" in _names(env.path)


async def test_a_delivery_failure_does_not_remove_the_entry(env):
    """If the bus rejects the send, the reminder is still owed."""
    reminders.add_entry(env.path, _reminder())
    env.bus.send.side_effect = RuntimeError("bus down")

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 0
    assert "reminder-old11111" in _names(env.path)


async def test_one_bad_role_does_not_stop_the_others(env):
    reminders.add_entry(env.butler_path, _reminder("reminder-bbb22222"))
    env.runtime.role_configs = {"ghost": object(), "butler": object()}

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1


async def test_sweeping_twice_delivers_once(env):
    reminders.add_entry(env.path, _reminder())

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1
    assert await reminders.sweep_reminders(env.runtime, NOW) == 0


async def test_an_entry_with_no_prompt_is_left_in_place(env):
    """Refuse rather than send an empty message and delete the evidence."""
    reminders.add_entry(env.path, _reminder(prompt="   "))

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 0
    env.bus.send.assert_not_awaited()
    assert "reminder-old11111" in _names(env.path)


async def test_a_date_entry_missing_one_shot_is_still_swept(env):
    """Defence in depth: membership is decided on type alone, so an entry that
    somehow lacked the flag is not skipped by BOTH registration and the sweep
    and thereby lost.

    Seeded directly rather than through ``add_entry``, which validates and would
    refuse this shape. The sweep's tolerance is a backstop for a file that
    acquired the shape some other way, not a shape the writer can produce —
    ``tests/test_reminders_store.py`` pins the refusal itself.
    """
    entry = _reminder()
    del entry["one_shot"]
    _seed(env.path, [entry])

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1


# ---------------------------------------------------------------------------
# The operator's entries share this file now — ownership is the only boundary
# ---------------------------------------------------------------------------


async def test_operator_triggers_are_never_delivered(env):
    """No longer true by looking elsewhere: the heartbeat is in the very file
    the sweep reads. Only the absent ``managed_by`` keeps it out."""
    assert await reminders.sweep_reminders(env.runtime, NOW) == 0
    env.bus.send.assert_not_awaited()
    assert _names(env.path) == ["heartbeat"]


async def test_an_operator_lookalike_is_neither_delivered_nor_removed(env):
    """THE negative case, and the one the live probe repeats. A hand-authored
    ``reminder-``-prefixed past-dated one-shot carries every mark that three
    rounds of #396 findings inferred ownership from."""
    _seed(env.path, [HEARTBEAT, _operators_lookalike()])
    before = pathlib.Path(env.path).read_bytes()

    assert await reminders.sweep_reminders(env.runtime, NOW) == 0

    env.bus.send.assert_not_awaited()
    assert pathlib.Path(env.path).read_bytes() == before


async def test_operator_entries_survive_a_sweep_that_does_deliver(env):
    """The write path is the risky one: removing a reminder rewrites the whole
    document, so this is where an operator entry would be lost."""
    import agent_loader
    _seed(env.path, [HEARTBEAT, _operators_lookalike()])
    reminders.add_entry(env.path, _reminder())
    before = [t for t in agent_loader._read_yaml(env.path)["triggers"]
              if t.get("managed_by") != "agent"]

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1

    after = agent_loader._read_yaml(env.path)["triggers"]
    assert after == before, "operator entries must be untouched, in order"


# ---------------------------------------------------------------------------
# Exclusive ownership between the scheduler and the sweep
# ---------------------------------------------------------------------------


async def test_a_reminder_with_a_live_job_is_not_swept(env):
    """The scheduler still owns it and WILL deliver it. Without this the two
    race for a reminder whose time has just passed and it arrives twice."""
    from config import TriggerSpec

    reminders.add_entry(env.path, _reminder())
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-old11111", type="cron", schedule="0 7 * * thu",
        channel="telegram", prompt="x")], ["telegram"])

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 0
    env.bus.send.assert_not_awaited()
    assert "reminder-old11111" in _names(env.path)


async def test_an_overdue_reminder_without_a_live_job_is_swept(env):
    """The post-restart case: jobs are memory-only, so a past-dated reminder
    has no job and the sweep is the only thing that can deliver it."""
    reminders.add_entry(env.path, _reminder())

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1


# ---------------------------------------------------------------------------
# The sweep as a convergence loop — the file is the truth
# ---------------------------------------------------------------------------


async def test_a_recurring_reminder_without_a_job_is_reregistered(env):
    """A reload that re-registers a role from a snapshot taken before the
    reminder was written drops its job. Only one-shots are recoverable by
    delivery, so without this a recurring reminder would never fire again until
    the next restart."""
    env.runtime.role_configs = _channels("assistant")
    reminders.add_entry(env.path, {
        "name": "reminder-rec33333", "type": "cron", "schedule": "0 7 * * thu",
        "at": "2099-08-06T07:00:00+02:00", "one_shot": False,
        "channel": "telegram", "prompt": "Gym.", "managed_by": "agent"})

    await reminders.sweep_reminders(env.runtime, NOW)

    assert env.registry.has_job("assistant", "reminder-rec33333")


async def test_a_future_one_shot_without_a_job_is_reregistered(env):
    env.runtime.role_configs = _channels("assistant")
    reminders.add_entry(env.path, _reminder("reminder-fut44444", at=LATER))

    await reminders.sweep_reminders(env.runtime, NOW)

    assert env.registry.has_job("assistant", "reminder-fut44444")


async def test_a_past_one_shot_is_delivered_not_reregistered(env):
    env.runtime.role_configs = _channels("assistant")
    reminders.add_entry(env.path, _reminder())

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1
    assert not env.registry.has_job("assistant", "reminder-old11111")


async def test_reconciliation_is_idempotent(env):
    env.runtime.role_configs = _channels("assistant")
    reminders.add_entry(env.path, {
        "name": "reminder-rec33333", "type": "cron", "schedule": "0 7 * * thu",
        "at": "2099-08-06T07:00:00+02:00", "one_shot": False,
        "channel": "telegram", "prompt": "Gym.", "managed_by": "agent"})

    await reminders.sweep_reminders(env.runtime, NOW)
    await reminders.sweep_reminders(env.runtime, NOW)   # must not raise

    assert env.registry.has_job("assistant", "reminder-rec33333")


async def test_an_unregisterable_entry_does_not_stop_the_others(env):
    """An entry naming a channel the role does not declare must not abort
    reconciliation for the rest."""
    env.runtime.role_configs = _channels("assistant")
    reminders.add_entry(env.path, {
        "name": "reminder-bad55555", "type": "cron", "schedule": "0 7 * * thu",
        "one_shot": False, "channel": "voice", "prompt": "x",
        "managed_by": "agent"})
    reminders.add_entry(env.path, {
        "name": "reminder-ok666666", "type": "cron", "schedule": "0 7 * * thu",
        "one_shot": False, "channel": "telegram", "prompt": "x",
        "managed_by": "agent"})

    await reminders.sweep_reminders(env.runtime, NOW)

    assert not env.registry.has_job("assistant", "reminder-bad55555")
    assert env.registry.has_job("assistant", "reminder-ok666666")


async def test_an_operator_entry_is_never_registered_by_the_sweep(env):
    """Direction 2 is bounded to agent-owned entries. Registering the
    operator's own trigger here would duplicate the job the boot path already
    made — and ``register_agent`` raises on a duplicate id."""
    env.runtime.role_configs = _channels("assistant")
    _seed(env.path, [HEARTBEAT, _operators_lookalike(
        "reminder-maintenance", at="2099-08-06T07:00:00+02:00")])

    await reminders.sweep_reminders(env.runtime, NOW)

    assert not env.registry.has_job("assistant", "heartbeat")
    assert not env.registry.has_job("assistant", "reminder-maintenance")


async def test_a_job_with_no_entry_left_is_dropped(env):
    """A cancellation that raced a reload — which re-registers the role from a
    snapshot taken before the cancellation — would otherwise leave the reminder
    firing forever despite cancel_reminder reporting success."""
    from config import TriggerSpec

    env.runtime.role_configs = _channels("assistant")
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-ghost01", type="cron", schedule="0 7 * * thu",
        channel="telegram", prompt="x", managed_by="agent")], ["telegram"])
    assert env.registry.has_job("assistant", "reminder-ghost01")

    await reminders.sweep_reminders(env.runtime, NOW)

    assert not env.registry.has_job("assistant", "reminder-ghost01")


async def test_operator_jobs_are_never_dropped_by_reconciliation(env):
    from config import TriggerSpec

    env.runtime.role_configs = _channels("assistant")
    env.registry.register_agent("assistant", [TriggerSpec(
        name="heartbeat", type="interval", minutes=60,
        channel="telegram", prompt="hb")], ["telegram"])

    await reminders.sweep_reminders(env.runtime, NOW)

    assert env.registry.has_job("assistant", "heartbeat")


async def test_ownership_not_the_name_decides_what_may_be_dropped(env):
    """Round 7 of #396 (both reviewers, third round on this one function). Two
    jobs with the SAME name shape and opposite ownership: the operator's must
    survive, the orphan must go. Nothing about the name can distinguish them."""
    from config import TriggerSpec

    env.runtime.role_configs = _channels("assistant")
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-operator", type="cron", schedule="0 3 * * sun",
        channel="telegram", prompt="operator")], ["telegram"])
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-orphaned", type="cron", schedule="0 3 * * sun",
        channel="telegram", prompt="ours", managed_by="agent")], ["telegram"])

    await reminders.sweep_reminders(env.runtime, NOW)

    assert env.registry.has_job("assistant", "reminder-operator")
    assert not env.registry.has_job("assistant", "reminder-orphaned")


async def test_an_operator_authored_lookalike_job_survives(env):
    """The operator declares a `reminder-`-prefixed trigger in their own file
    and it is registered from the boot path — carrying no ``managed_by``.
    Dropping its job because the agent owns no such entry would stop it firing
    entirely."""
    from config import TriggerSpec

    env.runtime.role_configs = _channels("assistant")
    _seed(env.path, [HEARTBEAT, {
        "name": "reminder-maintenance", "type": "cron",
        "schedule": "0 3 * * sun", "channel": "telegram",
        "prompt": "maintenance"}])
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-maintenance", type="cron", schedule="0 3 * * sun",
        channel="telegram", prompt="maintenance")], ["telegram"])

    await reminders.sweep_reminders(env.runtime, NOW)

    assert env.registry.has_job("assistant", "reminder-maintenance")


async def test_a_cleanup_failure_after_delivery_does_not_abort_the_pass(env):
    """Sol impl r1, the consequence that actually bites.

    A document nested ~400 levels deep PARSES but cannot be re-emitted, so the
    sweep delivers the reminder and then fails to remove it. Unfolded, that
    ``RecursionError`` escaped the sweep's ``(OSError, ValueError)`` handler and
    aborted the whole pass — so butler's overdue reminder went undelivered too,
    and assistant's was redelivered every five minutes.

    Red case: drop ``reminders._emit``'s fold and this returns 1, not 2.
    """
    env.runtime.role_configs = _channels("assistant", "butler")
    deep = "[" * 400 + "]" * 400
    with open(env.path, "w", encoding="utf-8") as fh:
        fh.write(
            "schema_version: 1\ntriggers:\n"
            f'  - {{name: reminder-aaa11111, type: date, at: "{OVERDUE}", '
            "one_shot: true, channel: telegram, prompt: x, managed_by: agent}\n"
            f"  - {{name: deep, type: interval, minutes: 1, "
            f"channel: telegram, prompt: {deep}}}\n")
    reminders.add_entry(env.butler_path, _reminder("reminder-bbb22222"))

    delivered = await reminders.sweep_reminders(env.runtime, NOW)

    assert delivered == 2, "butler must still be swept after assistant fails"
    assert "reminder-aaa11111" in _names(env.path), "still owed — not removed"


async def test_a_malformed_file_for_one_role_does_not_stop_another(env):
    """Newly load-bearing. The sweep now reads the operator's file, so a
    malformed one is no longer irrelevant — it must be contained to its own
    role rather than aborting the pass for everyone else."""
    env.runtime.role_configs = _channels("assistant", "butler")
    with open(env.path, "w", encoding="utf-8") as fh:
        fh.write("{{{ not: valid: yaml\n")
    reminders.add_entry(env.butler_path, _reminder("reminder-b0b0b0b0"))

    assert await reminders.sweep_reminders(env.runtime, NOW) == 1


async def test_an_unreadable_file_suspends_both_reconciliation_directions(env):
    """An empty list means "the agent owns nothing here", which authorises
    dropping every reminder job. A transient read error must never say that, or
    one bad read unschedules every recurring reminder."""
    from config import TriggerSpec

    env.runtime.role_configs = _channels("assistant")
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-live001", type="cron", schedule="0 7 * * thu",
        channel="telegram", prompt="x", managed_by="agent")], ["telegram"])
    with open(env.path, "w", encoding="utf-8") as fh:
        fh.write("- a list, not a mapping\n")

    await reminders.sweep_reminders(env.runtime, NOW)

    assert env.registry.has_job("assistant", "reminder-live001")


async def test_a_file_with_no_agent_entries_still_drops_orphans(env):
    """The unreadable sentinel must not disable reverse reconciliation for the
    ordinary case of a file holding only the operator's triggers."""
    from config import TriggerSpec

    env.runtime.role_configs = _channels("assistant")
    env.registry.register_agent("assistant", [TriggerSpec(
        name="reminder-orph01", type="cron", schedule="0 7 * * thu",
        channel="telegram", prompt="x", managed_by="agent")], ["telegram"])

    await reminders.sweep_reminders(env.runtime, NOW)

    assert not env.registry.has_job("assistant", "reminder-orph01")
