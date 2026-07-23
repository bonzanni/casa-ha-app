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


def test_owned_name_73_bytes_rejected():
    # Whole-branch H: OWNED_NAME_RE structurally admits 32 + 1 + 40 = 73 bytes,
    # one past the 72-byte scoped-name invariant. The byte bound must reject it.
    slug = "s" * 32
    mname = "m" * 40
    name = f"{slug}.{mname}"                       # 73 bytes
    assert len(name.encode()) == 73
    e = _entry(name=name, owner=f"specialist:{slug}", manifest_name=mname,
               targets=[f"specialist:{slug}"])
    e["artifact_id"] = compute_artifact_id(
        repo="bonzanni/casa-mtg-specialist", revision=REV,
        subdir="plugins/mtg", name=name)
    assert _entry_error(e) == "owned_invariant"
    # 72-byte name (39-char manifest) still valid.
    ok_name = f"{slug}.{'m' * 39}"
    assert len(ok_name.encode()) == 72
    ok = _entry(name=ok_name, owner=f"specialist:{slug}", manifest_name="m" * 39,
                targets=[f"specialist:{slug}"])
    ok["artifact_id"] = compute_artifact_id(
        repo="bonzanni/casa-mtg-specialist", revision=REV,
        subdir="plugins/mtg", name=ok_name)
    assert _entry_error(ok) is None


def test_apply_owned_swap_refuses_invalid_registry(tmp_path):
    # Whole-branch G: an unreadable/invalid registry must fail the swap closed —
    # never reconstruct a partial doc that drops pre-existing entries.
    import pytest
    reg = tmp_path / "registry.json"
    reg.write_text("{ this is not json")
    with pytest.raises(ValueError, match="registry_invalid"):
        plugin_registry.apply_owned_swap(
            slug="mtg", new_entries=[_entry()], registry_path=reg)
    # The bad file is left untouched (nothing saved over it).
    assert reg.read_text() == "{ this is not json"


def test_apply_owned_swap_clears_quarantine_flag(tmp_path):
    # Whole-branch L: a successful owned swap for a slug clears its stale
    # quarantined_bundles flag.
    reg = tmp_path / "registry.json"
    doc = _doc([])
    doc["quarantined_bundles"] = ["mtg", "finance"]
    reg.write_text(json.dumps(doc))
    plugin_registry.apply_owned_swap(
        slug="mtg", new_entries=[_entry()], registry_path=reg)
    saved = json.loads(reg.read_text())
    assert saved["quarantined_bundles"] == ["finance"]   # mtg cleared, finance kept


def test_resolved_plugin_constructor_regression():
    # A call site with nothing to thread (test fixtures; an unowned entry)
    # constructs ResolvedPlugin WITHOUT manifest_name — the field must
    # default, and runtime_name() must fall back to `name` when it is
    # absent. (Task 5 threads manifest_name through tools.py:905-907 and
    # 7755-7757 from real recorded/registry data — this test is about the
    # bare-constructor default, not those call sites.)
    rp = ResolvedPlugin(name="x", artifact_id="a" * 64, path="/p",
                        version="1", manifest={})
    assert rp.manifest_name == ""
    assert runtime_name(rp) == "x"
