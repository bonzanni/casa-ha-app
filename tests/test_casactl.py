"""Tests for casactl CLI script."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

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
