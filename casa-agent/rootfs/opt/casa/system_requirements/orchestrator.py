"""Ordered-fallback strategy selection across tarball/venv/npm backends
(§4.3 / §4.3.3). Produces a manifest entry per requirement for boot-time
reconciliation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .tarball import install_tarball, InstallResult
from .venv import install_venv
from .npm import install_npm

logger = logging.getLogger(__name__)

BackendFn = Callable[..., InstallResult]

DEFAULT_BACKENDS: Mapping[str, BackendFn] = {
    "tarball": install_tarball,
    "venv": install_venv,
    "npm": install_npm,
}

_REJECTED = {"apt", "dpkg", "yum", "dnf", "pacman"}


class OrchestrationError(RuntimeError):
    """Every strategy failed, or a rejected type was declared."""


@dataclass
class RequirementOutcome:
    requirement: dict
    winning_strategy: str
    install_dir: Path
    verify_bin: str
    pin_sha256: str | None = None
    pin_version: str | None = None

    def manifest_entry(self, plugin_name: str) -> dict:
        entry: dict = {
            "name": plugin_name,
            "winning_strategy": self.winning_strategy,
            "install_dir": str(self.install_dir),
            "verify_bin": self.verify_bin,
            "declared_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.pin_sha256:
            entry["pin_sha256"] = self.pin_sha256
        if self.pin_version:
            entry["pin_version"] = self.pin_version
        return entry


def install_requirements(
    *,
    plugin_name: str,
    requirements: Iterable[dict],
    tools_root: Path,
    backends: Mapping[str, BackendFn] = DEFAULT_BACKENDS,
) -> list[RequirementOutcome]:
    """Ordered-fallback: iterate `requirements` in list-order. First strategy
    that produces a resolving verify_bin wins. If all fail, raise.

    Returns list with exactly ONE outcome (the winner) per invocation.
    """
    reqs = list(requirements)

    # Pre-check for rejected types (defense-in-depth vs marketplace_ops).
    for req in reqs:
        rtype = req.get("type")
        if rtype in _REJECTED:
            raise OrchestrationError(
                f"plugin {plugin_name!r} declares {rtype!r}-type systemRequirement, "
                "which Casa does not support pre-1.0.0 (§4.3.2)"
            )

    last_msg = ""
    for req in reqs:
        strategy = req.get("type")
        if strategy not in backends:
            last_msg = f"unknown strategy {strategy!r}"
            continue
        backend = backends[strategy]
        try:
            result = backend(plugin_name=plugin_name, spec=req, tools_root=tools_root)
        except Exception as exc:  # noqa: BLE001 — log + fall through to next strategy
            logger.warning(
                "plugin %s: strategy %s raised %s", plugin_name, strategy, exc,
            )
            last_msg = f"{strategy} raised: {exc}"
            continue

        if not result.ok or not result.verify_bin_resolves:
            last_msg = (
                f"{strategy} produced no resolving verify_bin "
                f"({req.get('verify_bin')!r}): {result.message}"
            )
            continue

        return [RequirementOutcome(
            requirement=req,
            winning_strategy=strategy,
            install_dir=result.install_dir,
            verify_bin=req.get("verify_bin", ""),
            pin_sha256=req.get("sha256"),
            pin_version=req.get("version"),
        )]

    raise OrchestrationError(
        f"plugin {plugin_name!r}: all strategies failed. last: {last_msg}"
    )
