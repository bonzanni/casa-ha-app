"""manifest.yaml reader/writer for /addon_configs/casa-agent/system-requirements.yaml."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from system_requirements.manifest import (
    add_plugin_entry as add_manifest_entry,
    remove_plugin_entry as remove_manifest_entry,
    read_manifest,
)

pytestmark = pytest.mark.unit


def test_roundtrip(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "system-requirements.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH", path)

    add_manifest_entry({
        "name": "p",
        "winning_strategy": "tarball",
        "install_dir": "/t/p-1.0",
        "verify_bin": "p",
        "pin_sha256": "a" * 64,
        "declared_at": "2026-04-24T00:00:00Z",
    })
    data = read_manifest()
    assert len(data["plugins"]) == 1
    assert data["plugins"][0]["name"] == "p"


def test_remove(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "m.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH", path)
    add_manifest_entry({"name": "p", "winning_strategy": "tarball",
                        "install_dir": "/t/p-1.0", "verify_bin": "p",
                        "declared_at": "2026-04-24T00:00:00Z"})
    remove_manifest_entry("p")
    assert read_manifest() == {"plugins": []}


def test_write_is_atomic_crash_keeps_original(tmp_path: Path, monkeypatch) -> None:
    """A crash BETWEEN the temp write and os.replace must leave the prior
    system-requirements.yaml intact (not truncated), preserving its
    crash-recovery purpose."""
    import atomic_io

    path = tmp_path / "system-requirements.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH", path)
    add_manifest_entry({"name": "p", "winning_strategy": "tarball",
                        "install_dir": "/t/p-1.0", "verify_bin": "p",
                        "declared_at": "2026-04-24T00:00:00Z"})
    before = path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash before replace")

    monkeypatch.setattr(atomic_io.os, "replace", boom)
    with pytest.raises(RuntimeError):
        add_manifest_entry({"name": "q", "winning_strategy": "venv",
                            "install_dir": "/t/venv-q", "verify_bin": "q",
                            "declared_at": "2026-04-24T00:00:00Z"})

    assert path.read_text(encoding="utf-8") == before
    data = read_manifest()
    assert [p["name"] for p in data["plugins"]] == ["p"]
    import os as _os
    leftovers = [f for f in _os.listdir(tmp_path) if f != "system-requirements.yaml"]
    assert leftovers == []
