"""Tests for trigger_registry.py — per-agent scheduling + webhooks."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bus():
    bus = MagicMock()
    bus.send = AsyncMock()
    return bus


def _make_scheduler():
    sched = MagicMock()
    sched.add_job = MagicMock()
    return sched


def _trigger_interval(name="hb", minutes=60, channel="telegram"):
    from config import TriggerSpec
    return TriggerSpec(name=name, type="interval", minutes=minutes,
                       channel=channel, prompt="tick")


def _trigger_cron(name="morning", schedule="0 7 * * *", channel="telegram"):
    from config import TriggerSpec
    return TriggerSpec(name=name, type="cron", schedule=schedule,
                       channel=channel, prompt="morning digest")


def _trigger_webhook(name="gh", path="/webhook/gh"):
    from config import TriggerSpec
    return TriggerSpec(name=name, type="webhook", path=path)


# ---------------------------------------------------------------------------
# TestInterval
# ---------------------------------------------------------------------------


class TestInterval:
    async def test_interval_registers_apscheduler_job(self):
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent(
            role="assistant",
            triggers=[_trigger_interval()],
            channels=["telegram", "webhook"],
        )

        sched.add_job.assert_called_once()
        kwargs = sched.add_job.call_args.kwargs
        assert kwargs["trigger"] == "interval"
        assert kwargs["minutes"] == 60
        assert kwargs["id"] == "assistant:hb"

    async def test_interval_job_sends_scheduled_message(self):
        from trigger_registry import TriggerRegistry
        from bus import MessageType

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent("assistant", [_trigger_interval()],
                           channels=["telegram"])

        # Extract the async callable APScheduler would invoke.
        func = sched.add_job.call_args.args[0]
        await func()

        bus.send.assert_awaited_once()
        msg = bus.send.call_args.args[0]
        assert msg.type == MessageType.SCHEDULED
        assert msg.target == "assistant"
        assert msg.channel == "telegram"
        assert msg.content == "tick"

    async def test_interval_chat_id_is_honcho_compliant(self):
        """Spec §3.1: scheduled trigger chat_id must satisfy Honcho's
        `[A-Za-z0-9_-]+` regex so build_session_key + honcho_session_id
        do not raise. Producer-validator drift was the v0.17.1
        regression — protect with a roundtrip assertion."""
        from trigger_registry import TriggerRegistry
        from session_registry import build_session_key

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent(
            "assistant",
            [_trigger_interval(name="heartbeat")],
            channels=["telegram"],
        )

        func = sched.add_job.call_args.args[0]
        await func()

        msg = bus.send.call_args.args[0]
        assert msg.context["chat_id"] == "interval-heartbeat"

        # Roundtrip: must not raise. This is the load-bearing check —
        # producer hyphen must match honcho_session_id's regex.
        key = build_session_key(msg.channel, msg.context["chat_id"])
        assert key == "telegram-interval-heartbeat"


# ---------------------------------------------------------------------------
# TestCron
# ---------------------------------------------------------------------------


class TestCron:
    async def test_cron_registers_apscheduler_job(self):
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent(
            "assistant", [_trigger_cron()], channels=["telegram"],
        )

        sched.add_job.assert_called_once()
        kwargs = sched.add_job.call_args.kwargs
        assert kwargs["trigger"] == "cron"
        assert kwargs["id"] == "assistant:morning"


# ---------------------------------------------------------------------------
# TestWebhook
# ---------------------------------------------------------------------------


class TestWebhook:
    async def test_webhook_registers_aiohttp_route(self):
        from trigger_registry import TriggerRegistry

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent(
            "assistant", [_trigger_webhook()], channels=["webhook"],
        )

        paths = [str(r.resource.canonical) for r in app.router.routes()]
        assert any("/webhook/gh" in p for p in paths)

    async def test_duplicate_webhook_path_rejected(self):
        from trigger_registry import TriggerRegistry, TriggerError

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        reg.register_agent("assistant", [_trigger_webhook()],
                           channels=["webhook"])
        with pytest.raises(TriggerError, match="/webhook/gh"):
            reg.register_agent("butler", [_trigger_webhook()],
                               channels=["webhook"])


# ---------------------------------------------------------------------------
# TestValidation
# ---------------------------------------------------------------------------


class TestValidation:
    async def test_duplicate_trigger_name_same_agent_rejected(self):
        from trigger_registry import TriggerRegistry, TriggerError

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        t1 = _trigger_interval(name="dup")
        t2 = _trigger_interval(name="dup")
        with pytest.raises(TriggerError, match="duplicate"):
            reg.register_agent("assistant", [t1, t2],
                               channels=["telegram"])

    async def test_interval_channel_must_be_registered_on_agent(self):
        from trigger_registry import TriggerRegistry, TriggerError

        sched = _make_scheduler()
        app = web.Application()
        bus = _make_bus()
        reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

        trig = _trigger_interval(channel="unknown_channel")
        with pytest.raises(TriggerError, match="channel"):
            reg.register_agent("assistant", [trig],
                               channels=["telegram"])
