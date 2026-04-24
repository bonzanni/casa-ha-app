"""Read/write /addon_configs/casa-agent/system-requirements.yaml (P-7 schema)."""
from __future__ import annotations

from pathlib import Path

import yaml

MANIFEST_PATH: Path = Path("/addon_configs/casa-agent/system-requirements.yaml")


def read_manifest() -> dict:
    if not MANIFEST_PATH.is_file():
        return {"plugins": []}
    data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data.get("plugins"), list):
        data["plugins"] = []
    return data


def _write(data: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


def add_plugin_entry(entry: dict) -> None:
    data = read_manifest()
    name = entry["name"]
    data["plugins"] = [p for p in data["plugins"] if p["name"] != name]
    data["plugins"].append(entry)
    _write(data)


def remove_plugin_entry(name: str) -> None:
    data = read_manifest()
    data["plugins"] = [p for p in data["plugins"] if p["name"] != name]
    _write(data)
