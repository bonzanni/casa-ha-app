"""Tier 2 specialist loader + durable delegation compatibility facade.

Symmetric with :mod:`session_registry` and :mod:`mcp_registry`.
Scans a directory for per-specialist YAML files, validates the Tier 2
shape (no channels, zero token budget, ephemeral session), honours the
new ``enabled: bool`` field, and exposes a
runtime lookup used by the ``delegate_to_agent`` framework tool.

Delegation lifecycle state belongs exclusively to :mod:`job_registry`.
The legacy methods in this module remain as a narrow facade for existing
sync/async delegation call sites while they migrate to the job-native API.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from config import AgentConfig
from job_registry import (
    DeliveryState,
    ExecutionState,
    JobRegistry,
    VoiceJob,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class DelegationRecord:
    """Legacy call-site input translated into a durable ``VoiceJob``."""

    id: str                          # UUID4
    agent: str                       # specialist name (role)
    started_at: float                # time.time()
    origin: dict[str, Any] = field(default_factory=dict)
    # origin carries the channel/chat_id/cid/role/user_text of the
    # delegating resident's turn so the late-completion NOTIFICATION
    # can be delivered back to the right user via the right channel.
    # Task 6 (spec §4.6): the legacy delegate task still owns this Permit via
    # its done callback. It is never copied into the durable job snapshot.
    permit: Any = None


@dataclass
class DelegationComplete:
    """Typed payload published on the bus as NOTIFICATION content when a
    delegation resolves (or fails, or restart-orphans)."""

    delegation_id: str
    agent: str
    status: str                                    # "ok" | "error"
    text: str = ""
    kind: str = ""                                 # error kind or "restart_orphan"
    message: str = ""
    origin: dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0
    # Task 6 (spec §4.6): True when the delegated output was clipped to
    # `_MAX_OUTPUT_CHARS` before this notification was assembled, so the
    # narrating resident can disclose the answer was cut short.
    output_truncated: bool = False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SpecialistRegistry:
    """Load Tier 2 specialists and facade legacy lifecycle calls."""

    def __init__(
        self,
        specialists_dir: str,
        tombstone_path: str | None = None,
        *,
        job_registry: JobRegistry | None = None,
    ) -> None:
        self._dir = specialists_dir
        self._configs: dict[str, AgentConfig] = {}
        self._disabled_names: set[str] = set()
        self._load_failures: list[tuple[str, str]] = []
        if job_registry is None:
            if tombstone_path is None:
                raise TypeError("job_registry or tombstone_path is required")
            # Backward-compatible construction for tests and older embedders.
            # Production injects the one boot-loaded registry explicitly.
            job_registry = JobRegistry(
                os.path.join(os.path.dirname(tombstone_path), "jobs.json"),
                tombstone_path,
            )
        self._job_registry = job_registry

    # -- Loading / validation -------------------------------------------------

    def load(self) -> None:
        """Scan ``self._dir`` for specialist directories and register valid ones.

        O-2b (v0.37.9): per-specialist failures are tracked in
        :attr:`_load_failures` (also retrievable via :meth:`load_failures`)
        so :mod:`reload` can surface them to ``casactl`` callers. One
        malformed specialist does not poison its siblings — see
        :func:`agent_loader.load_all_specialists`.
        """
        from agent_loader import LoadError, load_all_specialists

        self._configs.clear()
        self._disabled_names.clear()
        self._load_failures = []
        try:
            found, failed = load_all_specialists(self._dir)
        except LoadError as exc:
            # Collection-level error (e.g. non-directory under specialists/).
            logger.error("Specialist load failed at collection level: %s", exc)
            found, failed = {}, [("(collection)", str(exc))]

        for name, err in failed:
            logger.error(
                "Specialist %r failed to load: %s; other specialists continue",
                name, err,
            )
            self._load_failures.append((name, err))

        for role, cfg in found.items():
            if not self._validate_tier2_shape(cfg, role):
                continue
            if not cfg.enabled:
                logger.info("Specialist %r bundled but disabled", role)
                self._disabled_names.add(role)
                continue
            self._configs[role] = cfg
            logger.info("Specialist %r loaded (model=%s)", role, cfg.model)
            # D-2 (v0.69.7): emit the same Layer-5 capability line residents
            # log in Agent.__init__ — specialists never build an Agent (they
            # run via _build_specialist_options), so without this they had no
            # boot-time capability oracle for post-install verification.
            try:
                allowed = list(getattr(cfg.tools, "allowed", []) or [])
                logger.info(
                    "agent_capabilities role=%s model=%s enabled=%s tool_count=%d "
                    "tools=%s mcp_servers=%s",
                    cfg.role, getattr(cfg, "model", "?"),
                    getattr(cfg, "enabled", "?"),
                    len(allowed), sorted(allowed),
                    sorted(getattr(cfg, "mcp_server_names", []) or []),
                )
            except Exception:  # noqa: BLE001 — an observability line must never break load
                logger.warning("agent_capabilities log failed for specialist role=%s",
                               getattr(cfg, "role", "?"), exc_info=True)

        logger.info(
            "Specialists: enabled=%s disabled=%s failed=%s",
            sorted(self._configs.keys()),
            sorted(self._disabled_names),
            sorted(n for n, _ in self._load_failures),
        )

    def load_failures(self) -> list[tuple[str, str]]:
        """Return per-specialist load failures from the last :meth:`load`.

        Defensive copy — callers cannot mutate registry state. Each entry
        is ``(directory_name, error_message)``. Empty list means the last
        load saw no per-specialist errors.
        """
        return list(self._load_failures)

    def _validate_tier2_shape(
        self, cfg: AgentConfig, role: str,
    ) -> bool:
        if cfg.channels:
            logger.error(
                "Rejecting specialist %r: Tier 2 forbids non-empty 'channels:' "
                "(channels belong to Tier 1 residents in agents/).",
                role,
            )
            return False
        if cfg.session.strategy != "ephemeral":
            logger.error(
                "Rejecting specialist %r: session.strategy must be 'ephemeral' "
                "(got %r).", role, cfg.session.strategy,
            )
            return False
        return True

    def get(self, agent_name: str) -> AgentConfig | None:
        """Return the enabled specialist config, or None."""
        return self._configs.get(agent_name)

    def is_disabled(self, role: str) -> bool:
        """True if ``role`` is bundled but disabled in user config.

        Returns False for unknown roles and for enabled specialists.
        Disabled-but-known specialists are still distinguishable from
        unknown roles (memory is data, enablement is operational).
        """
        return role in self._disabled_names

    def disabled_roles(self) -> list[str]:
        """Return a sorted list of disabled specialist role names.

        Defensive copy — caller cannot mutate registry state.
        """
        return sorted(self._disabled_names)

    def all_configs(self) -> dict[str, "AgentConfig"]:
        """Return a snapshot of enabled specialist configs by role.

        Used at boot to build the merged role→AgentConfig registry that
        ``delegate_to_agent`` resolves against. Returns a defensive copy.
        """
        return dict(self._configs)

    # -- Durable delegation compatibility facade -------------------------

    @property
    def job_registry(self) -> JobRegistry:
        return self._job_registry

    def has_delegation(self, delegation_id: str) -> bool:
        job = self._job_registry.get(delegation_id)
        return bool(job and job.execution_state in {
            ExecutionState.ACCEPTED, ExecutionState.RUNNING,
        })

    async def register_delegation(self, record: DelegationRecord) -> None:
        await self._job_registry.load()
        origin = dict(record.origin)
        await self._job_registry.create(VoiceJob(
            id=record.id,
            parent_job_id=None,
            creating_role=str(origin.get("role") or "assistant"),
            specialist_role=record.agent,
            specialist_display_name=record.agent,
            creator_peer=str(origin.get("channel") or ""),
            creator_user_id=self._optional_str(origin.get("user_id")),
            scope_id=str(origin.get("chat_id") or origin.get("scope_id") or ""),
            origin_route_id=self._optional_str(
                origin.get("cid") or origin.get("route_id")),
            origin_device_id=self._optional_str(
                origin.get("device_id") or origin.get("origin_device_id")),
            task=str(origin.get("user_text") or ""),
            context="",
            created_at=float(record.started_at),
            started_at=float(record.started_at),
            terminal_at=None,
            expires_at=None,
            execution_state=ExecutionState.RUNNING,
            delivery_state=DeliveryState.NONE,
            result=None,
            failure=None,
            awaiting_input=False,
            continuable_until=None,
            delivery_sequence=0,
            delivery_attempt_id=None,
            lease_until=None,
            cancel_pending=False,
        ))

    # Task 6 (spec §4.6): these terminal transitions deliberately do NOT
    # release the concurrency permit. For a LAUNCHED sync/async delegation
    # the task's ``_permit_release_callback`` done-callback is the SOLE
    # authoritative release — it fires only when the task ACTUALLY ends
    # (honouring cancellation). ``cancel_delegation`` in particular is called
    # by the voice teardown after only a bounded wait (tools._voice_deadline_
    # exceeded), while the specialist task may still be unwinding; releasing
    # here would free the slot for a NEW delegation while the original is
    # still executing (idempotence cannot undo a premature release). Pre-
    # launch cancellation is covered by the lexical ``owned`` guard in
    # delegate_to_agent. (Interactive engagements, which have no task done-
    # callback, DO release in EngagementRegistry terminal transitions.)
    async def complete_delegation(self, delegation_id: str) -> None:
        await self._job_registry.load()
        await self._job_registry.finish_compat(delegation_id, "")

    async def fail_delegation(
        self, delegation_id: str, exc: Exception,
    ) -> None:
        await self._job_registry.load()
        await self._job_registry.fail_compat(delegation_id, exc)

    async def cancel_delegation(self, delegation_id: str) -> None:
        await self._job_registry.load()
        await self._job_registry.cancel(delegation_id)

    def orphans_from_disk(self) -> list[DelegationRecord]:
        """Compatibility view of already-loaded orphaned durable jobs.

        This method deliberately performs no file I/O.  Boot migration and
        restart recovery are owned by :class:`JobRegistry`.
        """
        return [
            DelegationRecord(
                id=job.id,
                agent=job.specialist_role,
                started_at=job.started_at or job.created_at,
                origin=self._origin_from_job(job),
            )
            for job in self._job_registry.all()
            if job.execution_state is ExecutionState.ORPHANED
        ]

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _origin_from_job(job: VoiceJob) -> dict[str, Any]:
        return {
            "role": job.creating_role,
            "channel": job.creator_peer,
            "chat_id": job.scope_id,
            "cid": job.origin_route_id or "",
            "device_id": job.origin_device_id or "",
            "user_id": job.creator_user_id,
            "user_text": job.task,
        }
