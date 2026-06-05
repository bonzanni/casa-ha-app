"""Assert the shipped marketplace-defaults catalog is schema-valid + complete."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CATALOG = Path("casa-agent/rootfs/opt/casa/defaults/marketplace-defaults/.claude-plugin/marketplace.json")

REQUIRED_PLUGINS = {"superpowers", "plugin-dev", "skill-creator",
                    "mcp-server-dev"}

# document-skills was removed in v0.46.3 (it is xlsx/docx/pptx/pdf document
# PROCESSING, not plugin-dev tooling, and was mis-bundled). Guard the removal.
FORBIDDEN_PLUGINS = {"document-skills"}


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


def test_catalog_omits_forbidden_plugins() -> None:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    names = {p["name"] for p in data["plugins"]}
    present = FORBIDDEN_PLUGINS & names
    assert not present, f"Forbidden plugins present: {present}"


def test_catalog_every_entry_is_pinned() -> None:
    """v0.46.3 froze the dev tooling — no floating sources. git-subdir entries
    must carry a sha; github entries must use a tag/sha ref, not a bare branch."""
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    for p in data["plugins"]:
        src = p["source"]
        if src.get("source") == "git-subdir":
            assert src.get("sha"), f"{p['name']}: git-subdir must be pinned to a sha"
        elif src.get("source") == "github":
            ref = src.get("ref", "")
            assert ref and ref not in {"main", "master", "HEAD"}, (
                f"{p['name']}: github ref must be a tag/sha, not a floating branch ({ref!r})"
            )
