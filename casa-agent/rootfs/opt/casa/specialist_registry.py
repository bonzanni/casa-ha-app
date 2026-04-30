"""Tier 2 specialist loader + delegation bookkeeping (Phase 3.1).

Symmetric with :mod:`session_registry` and :mod:`mcp_registry`.
Scans a directory for per-specialist YAML files, validates the Tier 2
shape (no channels, zero token budget, ephemeral session, no
scopes_owned), honours the new ``enabled: bool`` field, and exposes a
runtime lookup used by the ``delegate_to_agent`` framework tool.

Also holds the in-flight delegation table (in-memory + ``/data/
delegations.json`` tombstone) consumed by the completion callback and
by startup orphan recovery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from config import AgentConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class DelegationRecord:
    """A single in-flight delegation.

    Stored in memory during the delegation lifetime; also tombstoned to
    disk so orphans can be recovered after a Casa restart.
    """

    id: str                          # UUID4
    agent: str                       # specialist name (role)
    started_at: float                # time.time()
    origin: dict[str, Any] = field(default_factory=dict)
    # origin carries the channel/chat_id/cid/role/user_text of the
    # delegating resident's turn so the late-completion NOTIFICATION
    # can be delivered back to the right user via the right channel.


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


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SpecialistRegistry:
    """Loads Tier 2 specialists and tracks in-flight delegations."""

    def __init__(self, specialists_dir: str, tombstone_path: str) -> None:
        self._dir = specialists_dir
        self._tombstone_path = tombstone_path
        self._configs: dict[str, AgentConfig] = {}
        self._disabled_names: set[str] = set()
        self._delegations: dict[str, DelegationRecord] = {}
        self._lock = asyncio.Lock()

    # -- Loading / validation -------------------------------------------------

    def load(self) -> None:
        """Scan ``self._dir`` for specialist directories and register valid ones."""
        from agent_loader import LoadError, load_all_specialists

        self._configs.clear()
        self._disabled_names.clear()
        try:
            found = load_all_specialists(self._dir)
        except LoadError as exc:
            logger.error("Specialist load failed: %s", exc)
            found = {}

        for role, cfg in found.items():
            if not self._validate_tier2_shape(cfg, role):
                continue
            if not cfg.enabled:
                logger.info("Specialist %r bundled but disabled", role)
                self._disabled_names.add(role)
                continue
            self._configs[role] = cfg
            logger.info("Specialist %r loaded (model=%s)", role, cfg.model)

        logger.info(
            "Specialists: enabled=%s disabled=%s",
            sorted(self._configs.keys()),
            sorted(self._disabled_names),
        )

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
        if cfg.memory.scopes_owned:
            logger.error(
                "Rejecting specialist %r: memory.scopes_owned must be empty "
                "(specialists own no scope).", role,
            )
            return False
        return True

    def get(self, agent_name: str) -> AgentConfig | None:
        """Return the enabled specialist config, or None."""
        return self._configs.get(agent_name)

    def is_disabled(self, role: str) -> bool:
        """True if ``role`` is bundled but disabled in user config.

        Returns False for unknown roles and for enabled specialists.
        Used by ``consult_other_agent_memory`` (M6) to fall through to
        cross_peer_context for disabled-but-known specialists — memory
        is data, enablement is operational.
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

    # -- Delegation bookkeeping (in-memory; tombstone in Task 5) ----------

    def has_delegation(self, delegation_id: str) -> bool:
        return delegation_id in self._delegations

    async def register_delegation(self, record: DelegationRecord) -> None:
        async with self._lock:
            self._delegations[record.id] = record
            await self._write_tombstone_locked()

    async def complete_delegation(self, delegation_id: str) -> None:
        async with self._lock:
            self._delegations.pop(delegation_id, None)
            await self._write_tombstone_locked()

    async def fail_delegation(
        self, delegation_id: str, exc: Exception,
    ) -> None:
        async with self._lock:
            self._delegations.pop(delegation_id, None)
            await self._write_tombstone_locked()

    async def cancel_delegation(self, delegation_id: str) -> None:
        async with self._lock:
            self._delegations.pop(delegation_id, None)
            await self._write_tombstone_locked()

    # -- Tombstone I/O ---------------------------------------------------

    async def _write_tombstone_locked(self) -> None:
        """Persist the in-flight delegations dict. Caller MUST hold
        ``self._lock``."""
        snapshot = [
            {
                "id": r.id,
                "agent": r.agent,
                "started_at": r.started_at,
                "origin": dict(r.origin),
            }
            for r in self._delegations.values()
        ]
        try:
            await asyncio.to_thread(self._write_tombstone, snapshot)
        except Exception as exc:
            logger.warning(
                "Failed to persist delegation tombstone: %s "
                "(in-flight delegations remain in memory; orphan recovery "
                "may miss them if Casa restarts)", exc,
            )

    def _write_tombstone(self, snapshot: list[dict[str, Any]]) -> None:
        with open(self._tombstone_path, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2)

    def orphans_from_disk(self) -> list[DelegationRecord]:
        """Read the tombstone file. Returns any records left by a prior
        process (Casa restarted mid-delegation). Truncates the file
        afterward. Called exactly once per startup.

        Failure modes:
        - File missing: return [] silently.
        - File corrupt: log ERROR, truncate, return [].
        """
        if not os.path.exists(self._tombstone_path):
            return []
        try:
            with open(self._tombstone_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "Tombstone file corrupt or unreadable (%s): %s — truncating",
                self._tombstone_path, exc,
            )
            try:
                with open(self._tombstone_path, "w", encoding="utf-8") as fh:
                    json.dump([], fh)
            except OSError:
                pass
            return []
        if not isinstance(raw, list):
            logger.error(
                "Tombstone file %s is not a JSON array; truncating",
                self._tombstone_path,
            )
            try:
                with open(self._tombstone_path, "w", encoding="utf-8") as fh:
                    json.dump([], fh)
            except OSError:
                pass
            return []
        records: list[DelegationRecord] = []
        for row in raw:
            try:
                records.append(DelegationRecord(
                    id=row["id"],
                    agent=row["agent"],
                    started_at=float(row.get("started_at", 0.0)),
                    origin=dict(row.get("origin") or {}),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "Skipping malformed tombstone entry: %s", exc,
                )
        # Truncate so we don't re-post on the NEXT restart too.
        try:
            with open(self._tombstone_path, "w", encoding="utf-8") as fh:
                json.dump([], fh)
        except OSError as exc:
            logger.warning(
                "Failed to truncate tombstone file after orphan read: %s", exc,
            )
        return records
