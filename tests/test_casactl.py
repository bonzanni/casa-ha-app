"""Tests for casactl CLI script."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CASACTL = Path(__file__).resolve().parent.parent / "casa-agent" / \
          "rootfs" / "usr" / "local" / "bin" / "casactl"


def test_casactl_no_args_shows_usage():
    result = subprocess.run(
        [sys.executable, str(CASACTL)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "usage" in (result.stderr + result.stdout).lower()


def test_casactl_reload_missing_scope_errors():
    result = subprocess.run(
        [sys.executable, str(CASACTL), "reload"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "scope" in (result.stderr + result.stdout).lower()


def test_casactl_help_smoke():
    """Smoke: --help works and lists subcommands."""
    result = subprocess.run(
        [sys.executable, str(CASACTL), "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "reload" in result.stdout
    assert "restart-supervised" in result.stdout


def test_casactl_scope_choices_cover_all_registered_handlers():
    """L29: every scope registered in reload._HANDLERS must be accepted by
    casactl. Guards against future scopes (e.g. v0.47.0's config_sync) being
    forgotten in the CLI's argparse choices, even though the server-side
    /admin/reload path would accept them."""
    import reload as reload_mod  # conftest.py adds the code root to sys.path

    result = subprocess.run(
        [sys.executable, str(CASACTL), "reload", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    for scope in reload_mod._HANDLERS:
        assert scope in result.stdout, (
            f"casactl --scope choices missing registered scope {scope!r}"
        )
