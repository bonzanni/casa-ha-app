"""Read/write /config/system-requirements.yaml (P-7 schema)."""
from __future__ import annotations

from pathlib import Path

import yaml

from atomic_io import atomic_write_text

MANIFEST_PATH: Path = Path("/config/system-requirements.yaml")


def read_manifest() -> dict:
    if not MANIFEST_PATH.is_file():
        return {"plugins": []}
    # Sol round-5: tolerate a malformed/unreadable manifest — return an empty
    # view rather than raising, so a corrupt system-requirements.yaml can't make
    # plugin verification (which reads this) raise before health regeneration.
    try:
        data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {"plugins": []}
    if not isinstance(data, dict):
        return {"plugins": []}
    if not isinstance(data.get("plugins"), list):
        data["plugins"] = []
    return data


def _write(data: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic (temp-file + fsync + os.replace): a crash mid-write must not
    # undermine the manifest's crash-recovery purpose with a truncated file.
    atomic_write_text(MANIFEST_PATH, yaml.safe_dump(data, sort_keys=True))


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
