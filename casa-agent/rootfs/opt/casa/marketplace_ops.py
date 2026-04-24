"""User-marketplace mutations (Plan 4b §3 + §7.1).

All operations target `/addon_configs/casa-agent/marketplace/.claude-plugin/marketplace.json`.
The seed-managed `casa-plugins-defaults` marketplace is OUT OF BOUNDS
here by design — CC enforces read-only, Configurator tools never call this
module against it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

USER_MARKETPLACE_PATH: Path = Path(
    "/addon_configs/casa-agent/marketplace/.claude-plugin/marketplace.json"
)


class MarketplaceError(ValueError):
    """Raised on schema violations or duplicate/missing entries."""


def _read() -> dict:
    try:
        return json.loads(USER_MARKETPLACE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketplaceError(
            f"user marketplace unreadable at {USER_MARKETPLACE_PATH}: {exc}"
        ) from exc


def _write(data: dict) -> None:
    USER_MARKETPLACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_MARKETPLACE_PATH.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_user_marketplace() -> dict:
    data = _read()
    if not isinstance(data.get("plugins"), list):
        raise MarketplaceError("user marketplace: 'plugins' must be array")
    return data


def list_plugin_entries() -> list[dict]:
    return list(load_user_marketplace()["plugins"])


def add_plugin_entry(entry: dict) -> None:
    data = load_user_marketplace()
    name = entry.get("name")
    if not name:
        raise MarketplaceError("entry missing 'name'")
    if any(p["name"] == name for p in data["plugins"]):
        raise MarketplaceError(f"plugin {name!r} already exists in user marketplace")
    # P-10: reject apt-type systemRequirements at add-time.
    reqs = (entry.get("casa") or {}).get("systemRequirements") or []
    for req in reqs:
        if isinstance(req, dict) and req.get("type") == "apt":
            raise MarketplaceError(
                f"plugin {name!r} declares an apt-type systemRequirement, "
                "which Casa does not support pre-1.0.0 (see §4.3.2). Ask the "
                "plugin author for a tarball/venv/npm alternative."
            )
    data["plugins"].append(entry)
    _write(data)


def remove_plugin_entry(name: str) -> bool:
    data = load_user_marketplace()
    before = len(data["plugins"])
    data["plugins"] = [p for p in data["plugins"] if p["name"] != name]
    if len(data["plugins"]) == before:
        raise MarketplaceError(f"plugin {name!r} not found in user marketplace")
    _write(data)
    return True


def update_plugin_entry(name: str, *, new_ref: str | None = None,
                        new_version: str | None = None) -> None:
    data = load_user_marketplace()
    for entry in data["plugins"]:
        if entry["name"] == name:
            if new_ref is not None:
                entry.setdefault("source", {})["sha"] = new_ref
            if new_version is not None:
                entry["version"] = new_version
            _write(data)
            return
    raise MarketplaceError(f"plugin {name!r} not found in user marketplace")
