"""Ordered-fallback strategy selection + manifest recording."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from system_requirements.orchestrator import (
    OrchestrationError,
    install_requirements,
)
from system_requirements.tarball import InstallResult

pytestmark = pytest.mark.unit


def test_first_strategy_wins(tmp_path: Path) -> None:
    calls: list[str] = []

    def tarball_ok(**_):
        calls.append("tarball")
        return InstallResult(ok=True, verify_bin_resolves=True,
                             install_dir=tmp_path / "a", message="ok")

    def venv_unused(**_):
        calls.append("venv")  # must NOT run
        return InstallResult(ok=True, verify_bin_resolves=True,
                             install_dir=tmp_path / "b", message="ok")

    results = install_requirements(
        plugin_name="p",
        requirements=[
            {"type": "tarball", "url": "x", "sha256": "y", "verify_bin": "b"},
            {"type": "venv", "package": "p", "verify_bin": "b"},
        ],
        tools_root=tmp_path,
        backends={"tarball": tarball_ok, "venv": venv_unused, "npm": MagicMock()},
    )
    assert calls == ["tarball"]
    assert results[0].winning_strategy == "tarball"


def test_falls_through_on_failure(tmp_path: Path) -> None:
    def tarball_fail(**_):
        return InstallResult(ok=False, verify_bin_resolves=False,
                             install_dir=tmp_path, message="network error")

    def venv_ok(**_):
        return InstallResult(ok=True, verify_bin_resolves=True,
                             install_dir=tmp_path, message="ok")

    results = install_requirements(
        plugin_name="p",
        requirements=[
            {"type": "tarball", "url": "x", "sha256": "y", "verify_bin": "b"},
            {"type": "venv", "package": "p", "verify_bin": "b"},
        ],
        tools_root=tmp_path,
        backends={"tarball": tarball_fail, "venv": venv_ok, "npm": MagicMock()},
    )
    assert results[0].winning_strategy == "venv"


def test_all_strategies_fail_raises(tmp_path: Path) -> None:
    fail = lambda **_: InstallResult(
        ok=False, verify_bin_resolves=False, install_dir=tmp_path, message="x")
    with pytest.raises(OrchestrationError):
        install_requirements(
            plugin_name="p",
            requirements=[
                {"type": "tarball", "url": "x", "sha256": "y", "verify_bin": "b"},
                {"type": "venv", "package": "p", "verify_bin": "b"},
            ],
            tools_root=tmp_path,
            backends={"tarball": fail, "venv": fail, "npm": fail},
        )


def test_apt_type_hard_error(tmp_path: Path) -> None:
    """Defense-in-depth: even if marketplace_add_plugin's check failed,
    orchestrator still refuses to call any backend for apt."""
    with pytest.raises(OrchestrationError, match="apt"):
        install_requirements(
            plugin_name="p",
            requirements=[{"type": "apt", "package": "ffmpeg", "verify_bin": "ffmpeg"}],
            tools_root=tmp_path,
            backends={"tarball": MagicMock(), "venv": MagicMock(), "npm": MagicMock()},
        )
