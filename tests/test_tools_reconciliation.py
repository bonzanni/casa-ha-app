"""Boot-time reconciliation of /addon_configs/casa-agent/tools/ (§4.3.4)."""
from __future__ import annotations

import json
import shutil
import sys
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

RECONCILER = Path("casa-agent/rootfs/opt/casa/scripts/reconcile_system_requirements.py")


def _write_manifest(path: Path, entries: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"plugins": entries}), encoding="utf-8")


def test_noop_when_tools_present(tmp_path: Path) -> None:
    tools_root = tmp_path / "tools"
    (tools_root / "bin").mkdir(parents=True)
    fake = tools_root / "bin" / "fakebin"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)

    manifest = tmp_path / "m.yaml"
    _write_manifest(manifest, [{
        "name": "face-rec",
        "winning_strategy": "tarball",
        "install_dir": str(tools_root / "face-rec-1.0.0"),
        "verify_bin": "fakebin",
        "declared_at": "2026-04-24T00:00:00Z",
    }])
    status = tmp_path / "status.yaml"

    r = subprocess.run([sys.executable, str(RECONCILER),
                        "--manifest", str(manifest),
                        "--tools-root", str(tools_root),
                        "--status-file", str(status),
                        "--log-level", "warning"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = yaml.safe_load(status.read_text())
    assert data["results"][0]["status"] == "ready"


def test_exits_nonzero_on_degraded(tmp_path: Path) -> None:
    tools_root = tmp_path / "tools"
    tools_root.mkdir()

    manifest = tmp_path / "m.yaml"
    _write_manifest(manifest, [{
        "name": "broken",
        "winning_strategy": "tarball",
        "install_dir": str(tools_root / "broken-0.0.0"),
        "verify_bin": "nothere",
        "pin_sha256": "0" * 64,
        "declared_at": "2026-04-24T00:00:00Z",
    }])
    status = tmp_path / "status.yaml"

    r = subprocess.run([sys.executable, str(RECONCILER),
                        "--manifest", str(manifest),
                        "--tools-root", str(tools_root),
                        "--status-file", str(status),
                        "--log-level", "warning"],
                       capture_output=True, text=True)
    assert r.returncode != 0
    data = yaml.safe_load(status.read_text())
    assert data["results"][0]["status"] == "degraded"
