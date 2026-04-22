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

    def load(self) -> None:
        from agent_loader import LoadError, load_all_executors

        self._defs.clear()
        self._disabled.clear()

        base = os.path.dirname(self._dir)
        try:
            found = load_all_executors(base)
        except LoadError as exc:
            logger.error("Executor load failed: %s", exc)
            found = {}

        for type_name, defn in found.items():
            if not defn.enabled:
                logger.info("Executor %r bundled but disabled", type_name)
                self._disabled.add(type_name)
                continue
            self._defs[type_name] = defn
            logger.info(
                "Executor %r loaded (model=%s driver=%s)",
                type_name, defn.model, defn.driver,
            )

        logger.info(
            "Executors: enabled=%s disabled=%s",
            sorted(self._defs.keys()),
            sorted(self._disabled),
        )

    def get(self, type_name: str) -> ExecutorDefinition | None:
        return self._defs.get(type_name)

    def list_types(self) -> list[str]:
        return sorted(self._defs.keys())
