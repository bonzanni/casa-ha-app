"""#396 — set_reminder / cancel_reminder."""

from __future__ import annotations

import json
import types
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
import yaml
from aiohttp import web

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

FUTURE = "2099-08-03T08:00:00+02:00"
FUTURE_THURSDAY = "2099-08-06T07:00:00+02:00"   # 2099-08-06 is a Thursday


def _payload(res):
    return json.loads(res["content"][0]["text"])


def _seed(path):
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump({
            "schema_version": 1,
            "triggers": [{"name": "heartbeat", "type": "interval",
                          "minutes": 60, "channel": "telegram",
                          "prompt": "hb"}],
        }, fh, sort_keys=False)


@dataclass
class Env:
    agents_dir: str
    triggers_path: str
    butler_triggers_path: str
    registry: object
    scheduler: object


@pytest.fixture
def env(tmp_path, monkeypatch):
    import agent as agent_mod
    import reminders
    from tools import init_tools
    from trigger_registry import TriggerRegistry

    agents_dir = tmp_path / "agents"
    for role in ("assistant", "butler"):
        (agents_dir / role).mkdir(parents=True)
        _seed(agents_dir / role / "triggers.yaml")

    scheduler = MagicMock()
    scheduler.add_job = MagicMock()
    scheduler.remove_job = MagicMock()
    bus = MagicMock()

    def _remove_fired(role, name):
        reminders.remove_entry(
            reminders.triggers_path(str(agents_dir), role), name)

    registry = TriggerRegistry(scheduler=scheduler, app=web.Application(),
                               bus=bus, on_one_shot_fired=_remove_fired)

    # The tools read only ``cfg.channels``; a real AgentConfig needs a
    # role_artifact and buys nothing here. The full type is exercised by the
    # agent_loader suites.
    assistant = types.SimpleNamespace(role="assistant", channels=["telegram"])
    butler = types.SimpleNamespace(role="butler", channels=["telegram"])

    runtime = types.SimpleNamespace(
        agents_dir=str(agents_dir), bus=bus, trigger_registry=registry,
        role_configs={"assistant": assistant, "butler": butler},
    )

    init_tools(channel_manager=MagicMock(), bus=bus,
               specialist_registry=MagicMock(), mcp_registry=MagicMock(),
               trigger_registry=registry, runtime=runtime)

    token = agent_mod.origin_var.set(
        {"role": "assistant", "channel": "telegram"})
    try:
        yield Env(
            agents_dir=str(agents_dir),
            triggers_path=str(agents_dir / "assistant" / "triggers.yaml"),
            butler_triggers_path=str(agents_dir / "butler" / "triggers.yaml"),
            registry=registry, scheduler=scheduler,
        )
    finally:
        agent_mod.origin_var.reset(token)


def _entries(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["triggers"]


def _find(path, name):
    return [t for t in _entries(path) if t["name"] == name][0]


# ---------------------------------------------------------------------------
# set_reminder
# ---------------------------------------------------------------------------


async def test_one_shot_writes_a_date_entry(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({
        "at": FUTURE, "text": "🗑 Garbage day — get the bins out before pickup."}))

    assert out["status"] == "ok"
    assert out["name"].startswith("reminder-")
    entry = _find(env.triggers_path, out["name"])
    assert entry["type"] == "date"
    assert entry["one_shot"] is True
    assert entry["channel"] == "telegram"
    assert "Garbage day" in entry["prompt"]


async def test_the_prompt_is_imperative(env):
    """A scheduled turn may legitimately stay silent, so delivery must not be
    left to judgement — this is the morning-briefing lesson (v0.132.0)."""
    from tools import set_reminder

    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "Bins."}))
    prompt = _find(env.triggers_path, out["name"])["prompt"]
    assert prompt.lower().startswith("send this exact message")
    assert "Bins." in prompt


async def test_weekly_writes_a_cron_entry_with_a_day_name(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({
        "at": FUTURE_THURSDAY, "text": "Gym.", "repeat": "weekly"}))

    entry = _find(env.triggers_path, out["name"])
    assert entry["type"] == "cron"
    assert entry["schedule"] == "0 7 * * thu"
    assert entry["one_shot"] is False
    assert "at" not in entry


async def test_recurring_reminder_is_not_one_shot(env):
    from tools import set_reminder

    for repeat in ("daily", "weekdays", "weekly", "monthly"):
        out = _payload(await set_reminder.handler({
            "at": FUTURE_THURSDAY, "text": "x", "repeat": repeat}))
        assert out["status"] == "ok", repeat
        assert _find(env.triggers_path, out["name"])["one_shot"] is False


async def test_the_job_is_registered_immediately(env):
    """Live now, not at next boot."""
    from tools import set_reminder

    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "Bins."}))
    ids = [c.kwargs.get("id") for c in env.scheduler.add_job.call_args_list]
    assert f"assistant:{out['name']}" in ids


async def test_response_echoes_the_resolved_time_and_repeat(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "Bins."}))
    assert out["repeat"] == "none"
    assert out["at"].startswith("2099-08-03T08:00:00")


async def test_existing_triggers_are_untouched(env):
    from tools import set_reminder

    await set_reminder.handler({"at": FUTURE, "text": "Bins."})
    assert _entries(env.triggers_path)[0]["name"] == "heartbeat"


async def test_writes_only_to_the_calling_roles_own_file(env):
    """INV-TRIG-007: the bound that keeps this from being a general writer."""
    from tools import set_reminder

    await set_reminder.handler({"at": FUTURE, "text": "Bins."})
    assert all(not t["name"].startswith("reminder-")
               for t in _entries(env.butler_triggers_path))


async def test_rejects_an_unknown_repeat(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({
        "at": FUTURE, "text": "x", "repeat": "fortnightly"}))
    assert out["status"] == "error"
    assert _entries(env.triggers_path) == _entries(env.triggers_path)[:1]


async def test_rejects_a_naive_at(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({
        "at": "2099-08-03T08:00:00", "text": "x"}))
    assert out["status"] == "error"


async def test_rejects_a_time_in_the_past(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({
        "at": "2000-01-01T08:00:00+02:00", "text": "x"}))
    assert out["status"] == "error"
    assert len(_entries(env.triggers_path)) == 1


async def test_rejects_empty_text(env):
    from tools import set_reminder

    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "   "}))
    assert out["status"] == "error"


async def test_registration_failure_rolls_back_the_entry(env):
    """A reminder recorded but never registered would look set and never
    fire until the next boot. Fail closed instead."""
    from tools import set_reminder

    env.scheduler.add_job.side_effect = RuntimeError("scheduler is down")
    out = _payload(await set_reminder.handler({"at": FUTURE, "text": "Bins."}))

    assert out["status"] == "error"
    assert len(_entries(env.triggers_path)) == 1


async def test_refuses_outside_a_turn_context(env):
    import agent as agent_mod
    from tools import set_reminder

    token = agent_mod.origin_var.set({})
    try:
        out = _payload(await set_reminder.handler({"at": FUTURE, "text": "x"}))
    finally:
        agent_mod.origin_var.reset(token)
    assert out["status"] == "error"


# ---------------------------------------------------------------------------
# cancel_reminder
# ---------------------------------------------------------------------------


async def test_cancels_a_reminder_it_created(env):
    from tools import cancel_reminder, set_reminder

    created = _payload(await set_reminder.handler({
        "at": FUTURE, "text": "Bins."}))
    out = _payload(await cancel_reminder.handler({"name": created["name"]}))

    assert out["status"] == "ok"
    assert all(t["name"] != created["name"]
               for t in _entries(env.triggers_path))
    env.scheduler.remove_job.assert_called_with(
        f"assistant:{created['name']}")


async def test_cancel_refuses_a_non_reminder_name(env):
    """INV-TRIG-007 red case: operator triggers are not the agent's to delete."""
    from tools import cancel_reminder

    out = _payload(await cancel_reminder.handler({"name": "heartbeat"}))
    assert out["status"] == "error"
    assert out["kind"] == "not_authorized"
    assert any(t["name"] == "heartbeat" for t in _entries(env.triggers_path))


async def test_cancel_unknown_reminder_reports_not_found(env):
    from tools import cancel_reminder

    out = _payload(await cancel_reminder.handler({"name": "reminder-zzzzzz"}))
    assert out["status"] == "error"
    assert out["kind"] == "not_found"


async def test_cancel_refuses_outside_a_turn_context(env):
    import agent as agent_mod
    from tools import cancel_reminder

    token = agent_mod.origin_var.set({})
    try:
        out = _payload(await cancel_reminder.handler(
            {"name": "reminder-a1b2c3"}))
    finally:
        agent_mod.origin_var.reset(token)
    assert out["status"] == "error"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def test_both_tools_are_registered_on_the_mcp_surface():
    from tools import CASA_TOOLS

    names = {getattr(t, "name", None) for t in CASA_TOOLS}
    assert "set_reminder" in names
    assert "cancel_reminder" in names
