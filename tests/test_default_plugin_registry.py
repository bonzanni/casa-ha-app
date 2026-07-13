"""The checked-in default plugin registry (bundled build INPUT, spec 3.6):
every entry pins an exact commit, and its artifact_id must equal the §3.2
identity recomputed from (repo, revision, subdir, name) — a mismatch would
break the boot bundle import and the build helper's assertion."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin_registry import TARGET_RE, compute_artifact_id

pytestmark = pytest.mark.unit

_PATH = (Path(__file__).resolve().parent.parent / "casa-agent" / "rootfs"
         / "opt" / "casa" / "defaults" / "plugin-registry.json")
_EXPECTED = {"superpowers", "plugin-dev", "skill-creator", "mcp-server-dev",
             "context7"}


def _doc():
    return json.loads(_PATH.read_text(encoding="utf-8"))


def test_parses_with_schema_version_1():
    doc = _doc()
    assert doc["schema_version"] == 1


def test_all_expected_names_present_and_unique():
    names = [e["name"] for e in _doc()["plugins"]]
    assert set(names) == _EXPECTED
    assert len(names) == len(set(names))


def test_every_entry_pins_exact_commit():
    for e in _doc()["plugins"]:
        rev = e["source"]["revision"]
        assert rev.startswith("git:") and len(rev) == len("git:") + 40
        assert all(c in "0123456789abcdef" for c in rev[len("git:"):])


def test_artifact_ids_match_recomputed_identity():
    for e in _doc()["plugins"]:
        src = e["source"]
        recomputed = compute_artifact_id(
            repo=src["repo"], revision=src["revision"],
            subdir=src.get("subdir", ""), name=e["name"])
        assert e["artifact_id"] == recomputed, (
            f"{e['name']}: checked-in artifact_id drifted from identity")


def test_targets_are_well_formed():
    for e in _doc()["plugins"]:
        for t in e["targets"]:
            assert TARGET_RE.match(t), f"{e['name']}: bad target {t!r}"
