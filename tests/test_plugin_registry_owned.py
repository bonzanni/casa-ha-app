"""Task 3: owner + manifest_name + scoped-name registry invariant (spec §2)."""
import json
from pathlib import Path

import plugin_registry
from plugin_registry import (
    RegistryData, ResolvedPlugin, _entry_error, _validate_doc,
    compute_artifact_id, runtime_name,
)

REV = "git:" + "a" * 40


def _entry(name="mtg.mtg", owner="specialist:mtg", manifest_name="mtg",
           targets=None, **over):
    targets = targets if targets is not None else ["specialist:mtg"]
    e = {
        "name": name,
        "source": {"type": "github", "repo": "bonzanni/casa-mtg-specialist",
                   "ref": "v0.2.0", "revision": REV, "subdir": "plugins/mtg"},
        "artifact_id": compute_artifact_id(
            repo="bonzanni/casa-mtg-specialist", revision=REV,
            subdir="plugins/mtg", name=name),
        "version": "1.0.0", "targets": targets,
    }
    if owner is not None:
        e["owner"] = owner
    if manifest_name is not None:
        e["manifest_name"] = manifest_name
    e.update(over)
    return e


def _doc(entries):
    return {"schema_version": 1, "seeded_defaults": [], "plugins": entries}


def test_valid_owned_entry_accepted():
    assert _entry_error(_entry()) is None


def test_unowned_entry_unchanged():
    e = _entry(name="gmail", owner=None, manifest_name=None,
               targets=["resident:assistant"])
    e["artifact_id"] = compute_artifact_id(
        repo="bonzanni/casa-mtg-specialist", revision=REV,
        subdir="plugins/mtg", name="gmail")
    assert _entry_error(e) is None


def test_operator_dot_name_rejected():
    # An unowned entry may never carry a dotted name.
    e = _entry(owner=None, manifest_name=None)
    assert _entry_error(e) == "bad_name"


def test_owner_without_scoped_name_rejected():
    e = _entry(name="mtg", manifest_name="mtg")
    e["artifact_id"] = compute_artifact_id(
        repo="bonzanni/casa-mtg-specialist", revision=REV,
        subdir="plugins/mtg", name="mtg")
    assert _entry_error(e) == "owned_invariant"


def test_owner_prefix_mismatch_rejected():
    assert _entry_error(_entry(owner="specialist:finance")) == "owned_invariant"


def test_owner_wrong_targets_rejected():
    assert _entry_error(_entry(targets=["specialist:mtg", "resident:assistant"])) \
        == "owned_invariant"
    assert _entry_error(_entry(targets=["specialist:other"])) == "owned_invariant"


def test_owner_missing_manifest_name_rejected():
    assert _entry_error(_entry(manifest_name=None)) == "owned_invariant"


def test_owner_manifest_name_suffix_mismatch_rejected():
    assert _entry_error(_entry(manifest_name="other")) == "owned_invariant"


def test_owner_bad_owner_shape_rejected():
    assert _entry_error(_entry(owner="resident:mtg")) == "owned_invariant"


def test_per_target_manifest_name_collision_invalidates_owned_entry():
    # An owned mtg.mtg (runtime name "mtg") targeting specialist:mtg collides
    # with a legacy unowned "mtg" ALSO targeting specialist:mtg.
    legacy = _entry(name="mtg", owner=None, manifest_name=None)
    legacy["artifact_id"] = compute_artifact_id(
        repo="bonzanni/casa-mtg-specialist", revision=REV,
        subdir="plugins/mtg", name="mtg")
    entries, issues, valid = _validate_doc(_doc([legacy, _entry()]))
    assert valid
    kept = {e["name"] for e in entries}
    assert "mtg" in kept and "mtg.mtg" not in kept          # owned entry loses
    assert any(i.reason_code == "manifest_name_collision" for i in issues)


def test_resolved_plugin_carries_manifest_name(tmp_path, monkeypatch):
    # resolve path: owned entry resolves with manifest_name="mtg".
    store = tmp_path / "store"
    art = _entry()
    p = store / art["name"] / art["artifact_id"]
    (p / ".claude-plugin").mkdir(parents=True)
    (p / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "mtg", "version": "1.0.0"}))
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps(_doc([art])))
    monkeypatch.setattr(plugin_registry, "_snapshot", None)
    monkeypatch.setattr(
        "plugin_store.artifact_verdict", lambda *a, **k: None)
    plugin_registry.reload_snapshot(registry_path=reg, store_root=store)
    res = plugin_registry.resolve_for("specialist:mtg")
    assert res.plugins and res.plugins[0].name == "mtg.mtg"
    assert res.plugins[0].manifest_name == "mtg"


def test_resolved_plugin_constructor_regression():
    # Every pre-Task-3 call site constructs ResolvedPlugin WITHOUT
    # manifest_name (Terra plan-r1: tools.py:905-907, 7755-7757 + test
    # fixtures) — the field must default, and runtime_name() must fall back
    # to `name` when it is absent.
    rp = ResolvedPlugin(name="x", artifact_id="a" * 64, path="/p",
                        version="1", manifest={})
    assert rp.manifest_name == ""
    assert runtime_name(rp) == "x"
