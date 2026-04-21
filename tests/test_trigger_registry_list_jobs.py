"""Tests for TriggerRegistry.list_jobs_for — used by the get_schedule tool."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

pytestmark = pytest.mark.asyncio


def _bus():
    bus = MagicMock()
    bus.send = AsyncMock()
    return bus


def _trigger(name, type_, *, minutes=0, schedule="", channel="telegram", prompt="p"):
    from config import TriggerSpec
    return TriggerSpec(
        name=name, type=type_, minutes=minutes, schedule=schedule,
        channel=channel, prompt=prompt,
    )


class TestListJobsFor:
    async def test_filters_by_role_prefix(self):
        from trigger_registry import TriggerRegistry

        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        sched = AsyncIOScheduler(timezone=timezone.utc)
        sched.start(paused=True)
        try:
            app = web.Application()
            bus = _bus()
            reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

            reg.register_agent("assistant",
                [_trigger("hb", "interval", minutes=60)],
                channels=["telegram"])
            reg.register_agent("butler",
                [_trigger("daily", "cron", schedule="0 8 * * *")],
                channels=["telegram"])

            ellen_jobs = reg.list_jobs_for("assistant", within_hours=24)
            names = [j.name for j in ellen_jobs]
            assert names == ["hb"]

            tina_jobs = reg.list_jobs_for("butler", within_hours=24)
            names = [j.name for j in tina_jobs]
            assert names == ["daily"]
        finally:
            sched.shutdown(wait=False)

    async def test_filters_by_time_window(self):
        from trigger_registry import TriggerRegistry
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        sched = AsyncIOScheduler(timezone=timezone.utc)
        sched.start(paused=True)
        try:
            app = web.Application()
            bus = _bus()
            reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

            reg.register_agent("assistant",
                [_trigger("yearly", "cron", schedule="0 0 1 1 *")],
                channels=["telegram"])

            jobs = reg.list_jobs_for("assistant", within_hours=1)
            assert jobs == []
        finally:
            sched.shutdown(wait=False)

    async def test_schedule_desc_shape(self):
        from trigger_registry import TriggerRegistry
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        sched = AsyncIOScheduler(timezone=timezone.utc)
        sched.start(paused=True)
        try:
            app = web.Application()
            bus = _bus()
            reg = TriggerRegistry(scheduler=sched, app=app, bus=bus)

            reg.register_agent("assistant",
                [
                    _trigger("hb", "interval", minutes=30),
                    _trigger("morn", "cron", schedule="0 8 * * 1-5"),
                ],
                channels=["telegram"])

            jobs = sorted(
                reg.list_jobs_for("assistant", within_hours=24 * 365),
                key=lambda j: j.name,
            )
            by_name = {j.name: j for j in jobs}
            assert by_name["hb"].schedule_desc == "every 30m"
            assert by_name["morn"].schedule_desc == "0 8 * * 1-5"
        finally:
            sched.shutdown(wait=False)
