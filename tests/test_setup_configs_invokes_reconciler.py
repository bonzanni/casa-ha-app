"""Structural guard: setup-configs.sh invokes the config_sync reconciler
after git-repo init, and the dir-level seed_agent_dir block is gone.

Spec: docs/superpowers/specs/2026-06-08-config-sync-reconciler-design.md §3.7.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SETUP = Path("casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh")


def test_invokes_reconciler() -> None:
    src = SETUP.read_text(encoding="utf-8")
    assert "config_sync.py" in src, "reconciler invocation missing"


def test_reconciler_runs_after_git_init() -> None:
    src = SETUP.read_text(encoding="utf-8")
    git_init = src.find("git init -q")
    invoke = src.find("config_sync.py")
    assert git_init >= 0 and invoke >= 0
    assert invoke > git_init, "reconciler must run AFTER git repo init (commit-first)"


def test_old_dir_level_seed_block_removed() -> None:
    src = SETUP.read_text(encoding="utf-8")
    # The dir-level no-op seeder is replaced by the reconciler's per-file logic.
    assert "seed_agent_dir()" not in src, "stale seed_agent_dir helper still present"


def test_c1_relay_migration_retained() -> None:
    src = SETUP.read_text(encoding="utf-8")
    assert "c1-relay-migration: begin" in src, "c1-relay migration must be retained"
