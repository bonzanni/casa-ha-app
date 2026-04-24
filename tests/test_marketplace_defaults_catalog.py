"""Assert the shipped marketplace-defaults catalog is schema-valid + complete."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CATALOG = Path("casa-agent/rootfs/opt/casa/defaults/marketplace-defaults/.claude-plugin/marketplace.json")

REQUIRED_PLUGINS = {"superpowers", "plugin-dev", "skill-creator",
                    "mcp-server-dev", "document-skills"}


def test_catalog_parses() -> None:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert data["name"] == "casa-plugins-defaults"
    assert isinstance(data.get("plugins"), list)


def test_catalog_contains_required_plugins() -> None:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    names = {p["name"] for p in data["plugins"]}
    missing = REQUIRED_PLUGINS - names
    assert not missing, f"Missing default plugins: {missing}"


def test_catalog_every_entry_has_github_source() -> None:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    for p in data["plugins"]:
        src = p.get("source")
        assert isinstance(src, dict), f"{p['name']}: source must be object"
        assert src.get("source") in {"github", "url", "git-subdir", "npm"}, (
            f"{p['name']}: invalid source shape {src}"
        )


def test_catalog_no_todo_placeholders() -> None:
    raw = CATALOG.read_text(encoding="utf-8")
    assert "TODO-plan-write" not in raw, \
        "Task A.2 must resolve every <TODO-plan-write> in marketplace-defaults"
    assert "<TODO-pin>" not in raw, "Task A.2 must pin every sha"
