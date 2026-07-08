"""M25: npm strategy must namespace its prefix per plugin so a two-stage-commit
rollback of one plugin (shutil.rmtree(install_dir)) cannot wipe every other
npm-installed plugin's node_modules and dangle their symlinks."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from system_requirements.npm import install_npm

pytestmark = pytest.mark.unit


def test_npm_rollback_of_one_plugin_leaves_other_intact(tmp_path: Path, monkeypatch):
    bins = {"pkg-a": "a-bin", "pkg-b": "b-bin"}

    def fake_run(cmd, check, timeout):
        prefix = Path(cmd[cmd.index("--prefix") + 1])
        bin_dir = prefix / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        p = bin_dir / bins[cmd[-1]]
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)

    monkeypatch.setattr("system_requirements.npm.subprocess.run", fake_run)

    ra = install_npm(
        plugin_name="plug-a",
        spec={"package": "pkg-a", "verify_bin": "a-bin"},
        tools_root=tmp_path,
    )
    rb = install_npm(
        plugin_name="plug-b",
        spec={"package": "pkg-b", "verify_bin": "b-bin"},
        tools_root=tmp_path,
    )
    assert ra.ok and rb.ok
    # Install dirs must be namespaced per plugin.
    assert ra.install_dir != rb.install_dir

    # Simulate tools.py:_tool_install_casa_plugin stage-2 rollback of plugin B.
    shutil.rmtree(rb.install_dir, ignore_errors=True)

    # Plugin A's tool must still resolve to a real file.
    a_link = tmp_path / "bin" / "a-bin"
    assert a_link.is_symlink() and a_link.resolve().is_file()
