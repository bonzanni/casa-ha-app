"""Per-agent trigger registry.

Residents declare their own interval / cron / webhook triggers in
``<agent>/triggers.yaml``. :class:`TriggerRegistry` wires each one
to the shared APScheduler instance or the HTTP app at boot time.

Replaces the single global heartbeat block in ``casa_core.main`` that
only understood assistant-level scheduling.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from bus import BusMessage, MessageBus, MessageType
from config import TriggerSpec
from log_cid import new_cid

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)


class TriggerError(Exception):
    """Raised on any trigger-wiring conflict or invalid shape."""


class TriggerRegistry:
    def __init__(
        self,
        *,
        scheduler: "AsyncIOScheduler",
        app: web.Application,
        bus: MessageBus,
    ) -> None:
        self._scheduler = scheduler
        self._app = app
        self._bus = bus
        self._seen_job_ids: set[str] = set()
        self._seen_webhook_paths: set[str] = set()

    def register_agent(
        self,
        role: str,
        triggers: list[TriggerSpec],
        channels: list[str],
    ) -> None:
        """Wire every trigger for *role*. Raises :class:`TriggerError`
        on validation failure. Idempotent failure — a partial register
        leaves prior triggers in place but stops at the offending entry."""

        names_seen: set[str] = set()
        for trig in triggers:
            if trig.name in names_seen:
                raise TriggerError(
                    f"agent {role!r}: duplicate trigger name {trig.name!r}"
                )
            names_seen.add(trig.name)

            if trig.type in ("interval", "cron"):
                if trig.channel not in channels:
                    raise TriggerError(
                        f"agent {role!r} trigger {trig.name!r}: channel "
                        f"{trig.channel!r} not registered on this agent "
                        f"(channels={channels})"
                    )
                self._register_scheduled(role, trig)
            elif trig.type == "webhook":
                if trig.path in self._seen_webhook_paths:
                    raise TriggerError(
                        f"agent {role!r} trigger {trig.name!r}: webhook "
                        f"path {trig.path!r} already registered"
                    )
                self._register_webhook(role, trig)
                self._seen_webhook_paths.add(trig.path)
            else:
                raise TriggerError(
                    f"agent {role!r} trigger {trig.name!r}: unknown "
                    f"type {trig.type!r}"
                )

    def _register_scheduled(self, role: str, trig: TriggerSpec) -> None:
        job_id = f"{role}:{trig.name}"
        if job_id in self._seen_job_ids:
            raise TriggerError(
                f"duplicate scheduler job id {job_id!r}"
            )

        async def _fire() -> None:
            logger.info(
                "Trigger firing: agent=%s name=%s type=%s",
                role, trig.name, trig.type,
            )
            msg = BusMessage(
                type=MessageType.SCHEDULED,
                source="scheduler",
                target=role,
                content=trig.prompt,
                channel=trig.channel,
                context={
                    "chat_id": f"{trig.type}:{trig.name}",
                    "trigger": trig.name,
                    "cid": new_cid(),
                },
            )
            await self._bus.send(msg)

        if trig.type == "interval":
            self._scheduler.add_job(
                _fire, trigger="interval", minutes=trig.minutes, id=job_id,
            )
        else:  # cron
            # Parse the 5-field cron string. APScheduler uses kwargs.
            fields = trig.schedule.split()
            if len(fields) != 5:
                raise TriggerError(
                    f"agent {role!r} trigger {trig.name!r}: cron schedule "
                    f"must be a 5-field string; got {trig.schedule!r}"
                )
            minute, hour, day, month, day_of_week = fields
            self._scheduler.add_job(
                _fire, trigger="cron",
                minute=minute, hour=hour, day=day, month=month,
                day_of_week=day_of_week, id=job_id,
            )
        self._seen_job_ids.add(job_id)

    def _register_webhook(self, role: str, trig: TriggerSpec) -> None:
        async def _handler(request: web.Request) -> web.Response:
            logger.info(
                "Webhook trigger fired: agent=%s name=%s path=%s",
                role, trig.name, trig.path,
            )
            try:
                payload = await request.json()
            except Exception:
                payload = {}

            msg = BusMessage(
                type=MessageType.SCHEDULED,
                source="webhook",
                target=role,
                content=f"Webhook {trig.name!r} fired; payload: {payload}",
                channel="webhook",
                context={
                    "webhook_name": trig.name,
                    "chat_id": f"webhook:{trig.name}",
                    "cid": request.get("cid") or new_cid(),
                },
            )
            await self._bus.send(msg)
            return web.json_response({"status": "accepted"})

        self._app.router.add_post(trig.path, _handler)
