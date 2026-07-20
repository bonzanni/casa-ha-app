"""Tier 3 Executor type registry.

Mirrors :mod:`specialist_registry` structurally but without an in-flight
table - :mod:`engagement_registry` already owns runtime state.
"""

from __future__ import annotations

import logging
import os

from config import ExecutorDefinition

logger = logging.getLogger(__name__)


class ExecutorRegistry:
    """Loads Tier 3 Executor type definitions from ``<executors_dir>/<type>/``."""

    def __init__(self, executors_dir: str) -> None:
        self._dir = executors_dir
        self._defs: dict[str, ExecutorDefinition] = {}
        self._disabled: set[str] = set()
        # Disabled executors' definitions, kept so verify/health can validate a
        # plugin assigned to a currently-disabled executor (the executor being
        # off by config is not a plugin-health failure). NEVER exposed via get()
        # — engagement launch relies on get()==None to refuse a disabled type.
        self._disabled_defs: dict[str, ExecutorDefinition] = {}

    def load(self) -> None:
        """Load every executor type under ``<self._dir>``.

        Personality Phase A, Task 5: ``load_all_executors`` now also loads
        and cross-validates each type's canonical role artifact
        (``defaults/roles/executor/<type>/{role.yaml,doctrine.md}``) and
        attaches it as ``ExecutorDefinition.role_artifact``, reachable
        through ``get()``/``definition_any()`` like every other field. A
        missing or id/kind/slot-mismatched artifact is a per-executor
        failure isolated the same way a schema violation is — it does not
        prevent sibling executors from loading. Task 6 consumes
        ``role_artifact`` for model resolution and the role checksum;
        executors get no persona and no binding.
        """
        from agent_loader import LoadError, load_all_executors

        self._defs.clear()
        self._disabled.clear()
        self._disabled_defs.clear()

        base = os.path.dirname(self._dir)
        try:
            found, failed = load_all_executors(base)
        except LoadError as exc:
            # Collection-level error (e.g. executors_root unreadable).
            logger.error("Executor load failed at collection level: %s", exc)
            found, failed = {}, []

        # v0.37.1 B-1b: per-file failures don't poison siblings.
        for name, err in failed:
            logger.error(
                "Executor %r failed to load: %s; other executors continue",
                name, err,
            )

        for type_name, defn in found.items():
            if not defn.enabled:
                logger.info("Executor %r bundled but disabled", type_name)
                self._disabled.add(type_name)
                self._disabled_defs[type_name] = defn
                continue
            self._defs[type_name] = defn
            logger.info(
                "Executor %r loaded (model=%s driver=%s)",
                type_name, defn.model, defn.driver,
            )

        logger.info(
            "Executors: loaded=%s failed=%s disabled=%s",
            sorted(self._defs.keys()),
            sorted(n for n, _ in failed),
            sorted(self._disabled),
        )

    def get(self, type_name: str) -> ExecutorDefinition | None:
        return self._defs.get(type_name)

    def is_disabled(self, type_name: str) -> bool:
        """True iff ``type_name`` loaded but is config-disabled (enabled: false)."""
        return type_name in self._disabled

    def definition_any(self, type_name: str) -> ExecutorDefinition | None:
        """The definition whether the executor is ENABLED or DISABLED — for
        verify/health AND boot resume of EXISTING engagements (Task 8): a
        specialist disabled AFTER an engagement launched must still resume,
        so ``replay_undergoing_engagements`` resolves a brief-bearing record's
        executor through here, not ``get()``. A plugin assigned to a disabled
        executor can likewise still have its tools.allowed inspected.
        Engagement LAUNCH (a NEW engagement) must keep using ``get()`` (None
        for disabled) to refuse a disabled type."""
        return self._defs.get(type_name) or self._disabled_defs.get(type_name)

    def list_types(self) -> list[str]:
        return sorted(self._defs.keys())
