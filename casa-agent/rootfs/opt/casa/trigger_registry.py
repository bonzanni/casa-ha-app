"""Per-agent trigger registry.

Residents declare their own interval / cron / webhook triggers in
``<agent>/triggers.yaml``. :class:`TriggerRegistry` wires each one
to the shared APScheduler instance or the HTTP app at boot time.

Replaces the single global heartbeat block in ``casa_core.main`` that
only understood assistant-level scheduling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
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


@dataclass
class TriggerSummary:
    name: str
    type: str            # "interval" | "cron"
    schedule_desc: str   # "every 30m" or the raw 5-field cron
    next_fire: datetime  # tz-aware


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
        self._specs_by_job_id: dict[str, TriggerSpec] = {}
        # N-1 + N-2 (v0.36.0): per-boot allowlist of webhook trigger names
        # → role. The wildcard /webhook/{name} handler in casa_core consults
        # this to 404 unknown names and dispatch knowns to the registered
        # role. reregister_for evicts removed names by role.
        self._webhook_targets: dict[str, str] = {}
        self._webhook_names_by_role: dict[str, list[str]] = {}
        # Release A: per-trigger memory read-clearance (spec A1/A4), stamped
        # onto webhook_trigger turns so the recall gate reads at the declared
        # tier (default/floor "public", never "private").
        self._webhook_clearances: dict[str, str] = {}
        # Release A: per-trigger auth policy (spec A1) — mode/header/
        # tolerance_secs/secret_owner. The wildcard handler reads it to verify
        # the request with the right scheme + secret.
        self._webhook_auth_policies: dict[str, dict] = {}

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
                # Release A: webhook triggers are served EXCLUSIVELY by the
                # authenticated wildcard /webhook/{name} handler — the old
                # per-path route is gone, so ``path`` is neither served nor a
                # collision axis (v2 triggers carry path=""; a path check would
                # false-collide). Uniqueness is by trigger NAME only.
                owner = self._webhook_targets.get(trig.name)
                if owner is not None and owner != role:
                    raise TriggerError(
                        f"agent {role!r} trigger {trig.name!r}: webhook "
                        f"trigger name already registered by role {owner!r}"
                    )
                self._register_webhook(role, trig)
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
                    "chat_id": f"{trig.type}-{trig.name}",
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
        self._specs_by_job_id[job_id] = trig

    def _register_webhook(self, role: str, trig: TriggerSpec) -> None:
        # Release A: webhook triggers are served ONLY by the authenticated
        # wildcard /webhook/{name} handler in casa_core (per-trigger auth +
        # body cap + origin stamping + fresh-uuid one-shot). The old
        # per-path ``router.add_post(trig.path, …)`` route — which did NO
        # auth, NO body cap, and pinned chat_id=trig.name — is REMOVED (it
        # was an unauthenticated bypass; a v2 trigger's empty path even
        # registered an open ``POST /``). This method now only maintains the
        # name→role/clearance/auth allowlist the wildcard handler consults.
        self._webhook_targets[trig.name] = role
        self._webhook_names_by_role.setdefault(role, []).append(trig.name)
        self._webhook_clearances[trig.name] = (
            getattr(trig, "clearance", "public") or "public"
        )
        self._webhook_auth_policies[trig.name] = getattr(trig, "auth", None) or {
            "mode": "hmac_body", "header": "X-Webhook-Signature",
            "tolerance_secs": 300, "secret_owner": "casa",
        }

    def get_webhook_target(self, name: str) -> str | None:
        """Return the role registered for a webhook trigger ``name``,
        or ``None`` if no such trigger is currently registered. Consulted
        by the wildcard ``/webhook/{name}`` handler in casa_core to 404
        unknown names and dispatch knowns to the right role.
        """
        return self._webhook_targets.get(name)

    def get_clearance(self, name: str) -> str:
        """Return the declared memory read-clearance for webhook trigger
        ``name`` (default ``"public"``). Stamped onto the dispatched turn's
        ``_origin_clearance`` so the recall gate reads at the declared tier.
        """
        return self._webhook_clearances.get(name, "public")

    def get_auth_policy(self, name: str) -> dict | None:
        """Return the per-trigger auth policy for webhook ``name`` (mode/header/
        tolerance_secs/secret_owner), or ``None`` if the name is unregistered.
        The wildcard handler verifies the request with this policy."""
        return self._webhook_auth_policies.get(name)

    def reregister_for(
        self,
        role: str,
        triggers: list[TriggerSpec],
        channels: list[str],
    ) -> None:
        """Remove this role's existing APScheduler jobs and webhook paths,
        then re-wire from the supplied specs.

        Fail-closed: if re-registration raises, the agent is left with NO
        triggers (the unwind already happened). The caller should surface
        the error.
        """
        prefix = f"{role}:"
        to_drop = [
            jid for jid in list(self._seen_job_ids) if jid.startswith(prefix)
        ]
        for jid in to_drop:
            try:
                self._scheduler.remove_job(jid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("remove_job %s failed: %s", jid, exc)
            self._seen_job_ids.discard(jid)
            self._specs_by_job_id.pop(jid, None)

        # N-2 (v0.36.0): evict this role's webhook names from the
        # allowlist so a removed webhook trigger naturally 404s on the
        # wildcard handler post-reload.
        for name in self._webhook_names_by_role.get(role, []):
            # Only evict if THIS role still owns the name (register_agent
            # now rejects cross-role webhook name collisions, so this is
            # belt-and-braces).
            if self._webhook_targets.get(name) == role:
                self._webhook_targets.pop(name, None)
                self._webhook_clearances.pop(name, None)
                self._webhook_auth_policies.pop(name, None)
        self._webhook_names_by_role[role] = []

        self.register_agent(role, triggers, channels)

    def list_jobs_for(
        self, role: str, within_hours: int,
    ) -> list[TriggerSummary]:
        """Return summaries of this agent's scheduled jobs firing in the window.

        Sorted by next fire time ascending. Does not include webhook
        triggers (they have no schedule).
        """
        within_hours = max(1, min(720, int(within_hours)))
        now = datetime.now(self._scheduler.timezone)
        cutoff = now + timedelta(hours=within_hours)

        out: list[TriggerSummary] = []
        prefix = f"{role}:"
        for job in self._scheduler.get_jobs():
            if not job.id.startswith(prefix):
                continue
            next_fire = job.next_run_time
            if next_fire is None or next_fire > cutoff:
                continue
            trig = self._specs_by_job_id.get(job.id)
            if trig is None:
                continue
            if trig.type == "interval":
                schedule_desc = f"every {trig.minutes}m"
            elif trig.type == "cron":
                schedule_desc = trig.schedule
            else:
                continue
            out.append(TriggerSummary(
                name=trig.name,
                type=trig.type,
                schedule_desc=schedule_desc,
                next_fire=next_fire,
            ))

        out.sort(key=lambda s: s.next_fire)
        return out
