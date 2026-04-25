"""Tests for the seed-copy block in setup-configs.sh.

The seed-copy populates a fresh cc-home from the image-baked
/opt/claude-seed/ directory, replacing the v0.14.8 boot install loop
with a no-network operation.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


SETUP_CONFIGS = Path("casa-agent/rootfs/etc/s6-overlay/scripts/setup-configs.sh")


def _extract_seed_copy_block() -> str:
    """Pull the seed-copy block out of setup-configs.sh as a standalone
    sh fragment we can run against fixture dirs in tests."""
    src = SETUP_CONFIGS.read_text()
    start = src.find("# === seed-copy: begin")
    end = src.find("# === seed-copy: end")
    assert start >= 0 and end > start, \
        "seed-copy block markers missing in setup-configs.sh"
    return src[start:end]


@pytest.fixture
def seed_dir(tmp_path: Path) -> Path:
    """Mock /opt/claude-seed/ — minimal valid structure."""
    d = tmp_path / "claude-seed"
    (d / "cache" / "casa-plugins-defaults" / "superpowers" / "5.0.7").mkdir(parents=True)
    (d / "marketplaces" / "casa-plugins-defaults").mkdir(parents=True)
    (d / "installed_plugins.json").write_text('[{"id": "superpowers@casa-plugins-defaults"}]')
    (d / "known_marketplaces.json").write_text('{"casa-plugins-defaults": {}}')
    return d


@pytest.fixture
def cc_home(tmp_path: Path) -> Path:
    """Mock /addon_configs/casa-agent/cc-home — empty plugins dir."""
    d = tmp_path / "cc-home"
    (d / ".claude" / "plugins").mkdir(parents=True)
    return d


def test_seed_copy_creates_install_state(seed_dir: Path, cc_home: Path) -> None:
    """First boot: cc-home has no installed_plugins.json → seed-copy fires."""
    block = _extract_seed_copy_block()
    env = {"PATH": "/usr/bin:/bin",
           "SEED_DIR": str(seed_dir),
           "CC_HOME": str(cc_home)}
    subprocess.run(["sh", "-c", block], check=True, env=env, timeout=10)
    plugins_dir = cc_home / ".claude" / "plugins"
    assert (plugins_dir / "installed_plugins.json").exists()
    # Cache dir must be present (symlink or real dir — both acceptable)
    cache_target = plugins_dir / "cache" / "casa-plugins-defaults"
    assert cache_target.exists() or cache_target.is_symlink()


def test_seed_copy_idempotent(seed_dir: Path, cc_home: Path) -> None:
    """Second boot: cc-home already has installed_plugins.json → seed-copy is a no-op."""
    plugins_dir = cc_home / ".claude" / "plugins"
    sentinel = plugins_dir / "installed_plugins.json"
    sentinel.write_text('[{"existing": true}]')
    mtime_before = sentinel.stat().st_mtime

    block = _extract_seed_copy_block()
    env = {"PATH": "/usr/bin:/bin",
           "SEED_DIR": str(seed_dir),
           "CC_HOME": str(cc_home)}
    subprocess.run(["sh", "-c", block], check=True, env=env, timeout=10)

    assert sentinel.read_text() == '[{"existing": true}]', \
        "seed-copy clobbered existing installed_plugins.json"
    # mtime unchanged → file untouched
    assert sentinel.stat().st_mtime == mtime_before
