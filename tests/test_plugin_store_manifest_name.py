"""Task 4: registry-name vs manifest-name decoupling (spec §2.1)."""
import json
from pathlib import Path

import pytest

import plugin_store


def _tree(tmp_path, plugin_json_name="mtg", casa: dict | None = None):
    root = tmp_path / "tree"
    (root / ".claude-plugin").mkdir(parents=True)
    manifest = {"name": plugin_json_name, "version": "1.0.0"}
    if casa is not None:
        manifest["casa"] = casa
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    return root


def test_validate_manifest_owned_uses_manifest_name(tmp_path):
    root = _tree(tmp_path, "mtg")
    mf = plugin_store.validate_manifest(root, "mtg.mtg", manifest_name="mtg")
    assert mf["name"] == "mtg"


def test_validate_manifest_owned_mismatch_fails(tmp_path):
    root = _tree(tmp_path, "other")
    with pytest.raises(plugin_store.StoreError):
        plugin_store.validate_manifest(root, "mtg.mtg", manifest_name="mtg")


def test_validate_manifest_unowned_unchanged(tmp_path):
    root = _tree(tmp_path, "mtg")
    assert plugin_store.validate_manifest(root, "mtg")["name"] == "mtg"
    with pytest.raises(plugin_store.StoreError):
        plugin_store.validate_manifest(root, "gmail")


def test_publish_from_tree_and_verdict_roundtrip(tmp_path):
    root = _tree(tmp_path, "mtg")
    res = plugin_store.publish_from_tree(
        name="mtg.mtg", repo="bonzanni/casa-specialist-mtg", ref="v0.2.0",
        revision="git:" + "a" * 40, subdir="plugins/mtg", src_root=root,
        store_root=tmp_path / "store", staging_root=tmp_path / "staging",
        manifest_name="mtg")
    verdict = plugin_store.artifact_verdict(
        Path(res.path), name="mtg.mtg", repo="bonzanni/casa-specialist-mtg",
        revision="git:" + "a" * 40, subdir="plugins/mtg",
        artifact_id=res.artifact_id, manifest_name="mtg")
    assert verdict is None
    # Without manifest_name the same artifact must FAIL (name != plugin.json.name)
    assert plugin_store.artifact_verdict(
        Path(res.path), name="mtg.mtg", repo="bonzanni/casa-specialist-mtg",
        revision="git:" + "a" * 40, subdir="plugins/mtg",
        artifact_id=res.artifact_id) == "artifact_invalid"


# --- Trigger-threading regression (reviewer-mandated, Terra plan-r1) --------
#
# manifest_triggers' effective-name (`plg-<plugin>--<declared>`) length gate is
# the only OBSERVABLE signal that distinguishes "used the scoped registry
# name" from "used the runtime manifest name": a long scoped registry name
# (e.g. "mtg.some-owner-suffix") pushes the effective name over the 64-char
# cap while the short manifest name ("mtg") does not.
_LONG_REGISTRY_NAME = "mtg." + "y" * 15   # scoped registry name (owned form)
_SHORT_MANIFEST_NAME = "mtg"
_DECLARED = "d" * 50


def _triggers_casa(declared=_DECLARED):
    return {
        "triggers": [{
            "name": declared,
            "type": "webhook",
            "target": "resident:gary",
            "auth": {"mode": "static_header"},
        }]
    }


def test_validate_manifest_trigger_uses_manifest_name_not_registry_name(tmp_path):
    """An owned artifact's trigger validation uses the runtime manifest name:
    the scoped registry name alone would overflow the effective-name cap, but
    validate_manifest must not use it for trigger-name derivation."""
    root = _tree(tmp_path, _SHORT_MANIFEST_NAME, casa=_triggers_casa())
    mf = plugin_store.validate_manifest(
        root, _LONG_REGISTRY_NAME, manifest_name=_SHORT_MANIFEST_NAME)
    assert mf["name"] == _SHORT_MANIFEST_NAME


def test_validate_manifest_trigger_registry_name_alone_would_overflow(tmp_path):
    """Sanity check for the fixture above: using the scoped registry name AS
    the trigger-validation plugin name (the bug this task fixes) overflows
    the effective-name cap and raises triggers_invalid."""
    root = _tree(tmp_path, _LONG_REGISTRY_NAME, casa=_triggers_casa())
    with pytest.raises(plugin_store.StoreError) as exc:
        plugin_store.validate_manifest(root, _LONG_REGISTRY_NAME)
    assert exc.value.reason_code == "triggers_invalid"


def test_validate_manifest_trigger_unowned_unchanged(tmp_path):
    """Regression: a trigger-bearing UNOWNED artifact (no manifest_name)
    validates identically to before this task."""
    root = _tree(tmp_path, "gmail", casa=_triggers_casa())
    mf = plugin_store.validate_manifest(root, "gmail")
    assert mf["name"] == "gmail"


def test_artifact_verdict_trigger_uses_manifest_name_not_registry_name(tmp_path):
    """artifact_verdict's manifest_triggers call must also derive the
    effective name from manifest_name (runtime identity), never the scoped
    registry name — same overflow signal as validate_manifest above."""
    root = _tree(tmp_path, _SHORT_MANIFEST_NAME, casa=_triggers_casa())
    res = plugin_store.publish_from_tree(
        name=_LONG_REGISTRY_NAME, repo="o/r", ref="v1",
        revision="git:" + "a" * 40, subdir="", src_root=root,
        store_root=tmp_path / "store", staging_root=tmp_path / "staging",
        manifest_name=_SHORT_MANIFEST_NAME)
    verdict = plugin_store.artifact_verdict(
        Path(res.path), name=_LONG_REGISTRY_NAME, repo="o/r",
        revision="git:" + "a" * 40, subdir="", artifact_id=res.artifact_id,
        manifest_name=_SHORT_MANIFEST_NAME)
    assert verdict is None


def test_artifact_verdict_trigger_unowned_unchanged(tmp_path):
    """Regression: a trigger-bearing UNOWNED artifact still validates
    identically through artifact_verdict (no manifest_name passed)."""
    root = _tree(tmp_path, "gmail", casa=_triggers_casa())
    res = plugin_store.publish_from_tree(
        name="gmail", repo="o/r", ref="v1", revision="git:" + "a" * 40,
        subdir="", src_root=root, store_root=tmp_path / "store",
        staging_root=tmp_path / "staging")
    verdict = plugin_store.artifact_verdict(
        Path(res.path), name="gmail", repo="o/r", revision="git:" + "a" * 40,
        subdir="", artifact_id=res.artifact_id)
    assert verdict is None
