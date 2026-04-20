"""Tier 2 executor loader + delegation bookkeeping (Phase 3.1).

Symmetric with :mod:`session_registry` and :mod:`mcp_registry`.
Scans a directory for per-executor YAML files, validates the Tier 2
shape (no channels, zero token budget, ephemeral session, no
scopes_owned), honours the new ``enabled: bool`` field, and exposes a
runtime lookup used by the ``delegate_to_agent`` framework tool.

Also holds the in-flight delegation table (in-memory + ``/data/
delegations.json`` tombstone) consumed by the completion callback and
by startup orphan recovery.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from config import AgentConfig, load_agent_config

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
    agent: str                       # executor name (role)
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


class ExecutorRegistry:
    """Loads Tier 2 executors and tracks in-flight delegations."""

    def __init__(self, executors_dir: str, tombstone_path: str) -> None:
        self._dir = executors_dir
        self._tombstone_path = tombstone_path
        self._configs: dict[str, AgentConfig] = {}
        self._delegations: dict[str, DelegationRecord] = {}
        self._lock = asyncio.Lock()

    # -- Loading / validation -------------------------------------------------

    def load(self) -> None:
        """Scan ``self._dir`` for ``*.yaml`` and register valid executors."""
        self._configs.clear()
        if not os.path.isdir(self._dir):
            return
        for entry in sorted(os.listdir(self._dir)):
            if not entry.endswith(".yaml"):
                continue
            path = os.path.join(self._dir, entry)
            try:
                cfg = load_agent_config(path)
            except Exception as exc:
                logger.error(
                    "Failed to load executor %s: %s", path, exc,
                )
                continue
            if not self._validate_tier2_shape(cfg, entry):
                continue
            if not cfg.enabled:
                logger.info(
                    "Executor %r bundled but disabled (file=%s)",
                    cfg.role, entry,
                )
                continue
            self._configs[cfg.role] = cfg
            logger.info(
                "Executor %r loaded (model=%s)", cfg.role, cfg.model,
            )

    def _validate_tier2_shape(
        self, cfg: AgentConfig, entry: str,
    ) -> bool:
        if cfg.channels:
            logger.error(
                "Rejecting executor %s: Tier 2 forbids non-empty 'channels:' "
                "(channels belong to Tier 1 residents in agents/).",
                entry,
            )
            return False
        if cfg.session.strategy != "ephemeral":
            logger.error(
                "Rejecting executor %s: session.strategy must be 'ephemeral' "
                "(got %r).", entry, cfg.session.strategy,
            )
            return False
        if cfg.memory.scopes_owned:
            logger.error(
                "Rejecting executor %s: memory.scopes_owned must be empty "
                "(executors own no scope).", entry,
            )
            return False
        if cfg.memory.token_budget > 0:
            logger.error(
                "Rejecting executor %s: memory.token_budget must be 0 "
                "(executors are stateless).", entry,
            )
            return False
        return True

    def get(self, agent_name: str) -> AgentConfig | None:
        """Return the enabled executor config, or None."""
        return self._configs.get(agent_name)

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

    # -- Tombstone I/O — stubbed here; real impl in Task 5. --------------

    async def _write_tombstone_locked(self) -> None:
        """Overwritten in Task 5 with real atomic-write logic. No-op
        here so Task 4's lifecycle tests pass without needing disk."""
        return None
