"""§3.2 publish pipeline: ref resolution failure taxonomy, staging + atomic
rename, idempotent re-publish, corrupt-destination fail-closed, manifest
validation, bundle import."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import plugin_store
from plugin_registry import compute_artifact_id
from plugin_store import (
    METADATA_FILENAME,
    RefNotFound,
    ResolveUnavailable,
    StoreError,
    content_checksum,
    import_bundle,
    publish,
    publish_from_tree,
    resolve_ref,
    validate_artifact,
    validate_manifest,
)

pytestmark = pytest.mark.unit

SHA = "a" * 40


def _unfreeze(p: Path) -> None:
    """Restore write on a published artifact file — publish() now freezes files
    read-only (Sol #7). Tests that simulate corruption which BYPASSED the freeze
    (privileged process / disk error) must defeat it first; the artifact_verdict
    backstop must still catch the corruption."""
    import os
    import stat
    os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) | 0o200)


def _plugin_tree(tmp_path, name="probe", version="1.0.0") -> Path:
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8")
    (root / "skills").mkdir()
    (root / "skills" / "s.md").write_text("skill", encoding="utf-8")
    return root


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_resolve_ref_happy():
    with patch("plugin_store.subprocess.run",
               return_value=_Proc(0, json.dumps({"sha": SHA}))) as run:
        assert resolve_ref("o/r", "v1.0.0") == SHA
    argv = run.call_args[0][0]
    assert argv[:2] == ["gh", "api"]


def test_resolve_ref_404_is_ref_not_found():
    with patch("plugin_store.subprocess.run",
               return_value=_Proc(1, "", "gh: Not Found (HTTP 404)")):
        with pytest.raises(RefNotFound):
            resolve_ref("o/r", "phantom-tag")


def test_resolve_ref_network_is_resolve_unavailable():
    with patch("plugin_store.subprocess.run",
               return_value=_Proc(1, "", "error connecting to api.github.com")):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1")


def test_resolve_ref_timeout_is_resolve_unavailable():
    with patch("plugin_store.subprocess.run",
               side_effect=subprocess.TimeoutExpired(["gh"], 20)):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1")


def test_resolve_ref_missing_gh_is_resolve_unavailable():
    with patch("plugin_store.subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(ResolveUnavailable):
            resolve_ref("o/r", "v1")


def test_validate_manifest_paths(tmp_path):
    root = _plugin_tree(tmp_path)
    assert validate_manifest(root, "probe")["version"] == "1.0.0"
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "other-name")
    assert ei.value.reason_code == "name_mismatch"
    (root / ".claude-plugin" / "plugin.json").write_text("{broken",
                                                         encoding="utf-8")
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "probe")
    assert ei.value.reason_code == "manifest_invalid"


def test_validate_manifest_defaults_missing_version(tmp_path):
    """CI/real-world: plugins like anthropics/claude-plugins-official ship NO
    top-level version. validate_manifest must default it (0.0.0), not reject —
    version is no longer identity-load-bearing. The unit gate's versioned
    fixtures masked this; only the image build caught it."""
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "probe"}), encoding="utf-8")   # no version
    assert validate_manifest(root, "probe")["version"] == "0.0.0"


def test_validate_manifest_tolerates_non_object_casa(tmp_path):
    root = _plugin_tree(tmp_path)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "probe", "version": "1.0.0", "casa": "oops"}),
        encoding="utf-8")
    assert validate_manifest(root, "probe")["version"] == "1.0.0"


def test_validate_manifest_rejects_apt(tmp_path):
    root = _plugin_tree(tmp_path)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "probe", "version": "1.0.0",
        "casa": {"systemRequirements": [{"type": "apt", "package": "x"}]},
    }), encoding="utf-8")
    with pytest.raises(StoreError) as ei:
        validate_manifest(root, "probe")
    assert ei.value.reason_code == "apt_requirements_rejected"


def _wire_fetch(src_root):
    """publish() fetches into staging: fake fetch_commit_tree by copying."""
    import shutil

    def _fake(repo, commit, subdir, dest, **kw):
        shutil.copytree(src_root, dest, dirs_exist_ok=True, symlinks=True)
    return _fake


def test_publish_happy_atomic(tmp_path):
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        res = publish(name="probe", repo="o/r", ref="v1",
                      store_root=store, staging_root=staging)
    assert res.revision == f"git:{SHA}"
    assert res.version == "1.0.0"
    dest = store / "probe" / res.artifact_id
    assert Path(res.path) == dest and validate_artifact(dest)
    assert not any(staging.iterdir())          # staging cleaned


def test_publish_existing_valid_is_noop(tmp_path):
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        r1 = publish(name="probe", repo="o/r", ref="v1",
                     store_root=store, staging_root=staging)
        r2 = publish(name="probe", repo="o/r", ref="v1",
                     store_root=store, staging_root=staging)
    assert r1.artifact_id == r2.artifact_id


def test_publish_existing_corrupt_fails_closed(tmp_path):
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        r1 = publish(name="probe", repo="o/r", ref="v1",
                     store_root=store, staging_root=staging)
        # Tamper the published artifact (defeat the Sol #7 freeze to model
        # corruption that bypassed it — the verdict backstop must still catch it).
        _unfreeze(Path(r1.path) / "skills" / "s.md")
        (Path(r1.path) / "skills" / "s.md").write_text("evil", encoding="utf-8")
        with pytest.raises(StoreError) as ei:
            publish(name="probe", repo="o/r", ref="v1",
                    store_root=store, staging_root=staging)
    assert ei.value.reason_code == "corrupt_artifact"
    # Nothing swapped: tampered content still in place (operator/GC recovers).
    assert (Path(r1.path) / "skills" / "s.md").read_text(
        encoding="utf-8") == "evil"


def test_publish_existing_wrong_identity_metadata_fails_closed(tmp_path):
    """A destination whose checksum self-validates but whose metadata names a
    DIFFERENT identity is corrupt — never silently accepted."""
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        r1 = publish(name="probe", repo="o/r", ref="v1",
                     store_root=store, staging_root=staging)
        meta_path = Path(r1.path) / METADATA_FILENAME
        _unfreeze(meta_path)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["revision"] = "git:" + "b" * 40      # wrong identity
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        # re-fix the content checksum so ONLY identity is wrong
        meta["content_checksum"] = content_checksum(Path(r1.path))
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(StoreError) as ei:
            publish(name="probe", repo="o/r", ref="v1",
                    store_root=store, staging_root=staging)
    assert ei.value.reason_code == "corrupt_artifact"


def test_publish_failure_cleans_staging_store_unchanged(tmp_path):
    src = _plugin_tree(tmp_path, name="WRONG")  # name mismatch → validate fails
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        with pytest.raises(StoreError):
            publish(name="probe", repo="o/r", ref="v1",
                    store_root=store, staging_root=staging)
    assert not (store / "probe").exists()
    assert not staging.exists() or not any(staging.iterdir())


def test_publish_from_tree_excludes_git_and_uses_given_revision(tmp_path):
    src = _plugin_tree(tmp_path)
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: x", encoding="utf-8")
    store, staging = tmp_path / "store", tmp_path / "staging"
    rev = "legacy-content:" + "c" * 64
    res = publish_from_tree(name="probe", repo="o/r", ref="master",
                            revision=rev, subdir="", src_root=src,
                            store_root=store, staging_root=staging)
    assert res.revision == rev
    assert not (Path(res.path) / ".git").exists()
    expected = compute_artifact_id(repo="o/r", revision=rev, subdir="",
                                   name="probe")
    assert res.artifact_id == expected


def test_import_bundle_idempotent_and_fail_closed(tmp_path):
    src = _plugin_tree(tmp_path)
    bundle, store = tmp_path / "bundle", tmp_path / "store"
    res = publish_from_tree(name="probe", repo="o/r", ref="v1",
                            revision=f"git:{SHA}", subdir="", src_root=src,
                            store_root=bundle, staging_root=tmp_path / "stg")
    issues = import_bundle(bundle, store_root=store)
    assert issues == []
    dest = store / "probe" / res.artifact_id
    assert validate_artifact(dest)
    assert import_bundle(bundle, store_root=store) == []   # idempotent
    # Corrupt the store copy → issue raised, NOT silently replaced.
    _unfreeze(dest / "skills" / "s.md")
    (dest / "skills" / "s.md").write_text("evil", encoding="utf-8")
    issues = import_bundle(bundle, store_root=store)
    assert [i.reason_code for i in issues] == ["corrupt_artifact"]


def test_publish_freezes_artifact_files_readonly(tmp_path):
    """Sol #7: a published artifact's files are read-only (no write bit for any
    class) so in-place tampering can't defeat the cached deep-validation."""
    import os
    import stat
    src = _plugin_tree(tmp_path)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with patch("plugin_store.resolve_ref", return_value=SHA), \
         patch("plugin_store.fetch_commit_tree", side_effect=_wire_fetch(src)):
        r = publish(name="probe", repo="o/r", ref="v1",
                    store_root=store, staging_root=staging)
    skill = Path(r.path) / "skills" / "s.md"
    mode = stat.S_IMODE(os.lstat(skill).st_mode)
    assert mode & 0o222 == 0, f"artifact file still writable: {oct(mode)}"
    # verify_bin backstop still readable (deep validation must pass).
    from plugin_store import validate_artifact
    assert validate_artifact(Path(r.path))


def test_gc_disabled_returns_candidates_without_deleting(tmp_path):
    src = _plugin_tree(tmp_path)
    store = tmp_path / "store"
    res = publish_from_tree(name="probe", repo="o/r", ref="v1",
                            revision=f"git:{SHA}", subdir="", src_root=src,
                            store_root=store, staging_root=tmp_path / "stg")
    cands = plugin_store.gc_sweep(store_root=store, referenced=set(),
                                  min_age_days=0, enabled=False)
    assert cands == [res.artifact_id] and Path(res.path).exists()


def test_publish_from_tree_rejects_escaping_symlink(tmp_path):
    """Sol round-3 H7: an offline-adopt tree with a symlink escaping the artifact
    root is rejected (unsafe_archive) — freezing/loading it must never touch or
    expose an external file."""
    import os
    src = _plugin_tree(tmp_path)
    os.symlink("/etc/passwd", src / "evil-link")      # escaping absolute symlink
    store, staging = tmp_path / "store", tmp_path / "staging"
    with pytest.raises(StoreError) as ei:
        publish_from_tree(name="probe", repo="o/r", ref="master",
                          revision="legacy-content:" + "c" * 64, subdir="",
                          src_root=src, store_root=store, staging_root=staging)
    assert ei.value.reason_code == "unsafe_archive"
    assert not (store / "probe").exists()             # nothing published


def test_publish_from_tree_allows_internal_symlink(tmp_path):
    """Sol round-3 H7: an in-artifact symlink (non-escaping) is allowed; freeze
    skips it without chmod-following."""
    import os
    src = _plugin_tree(tmp_path)
    (src / "skills" / "target.md").write_text("t", encoding="utf-8")
    os.symlink("target.md", src / "skills" / "link.md")   # internal, relative
    store, staging = tmp_path / "store", tmp_path / "staging"
    res = publish_from_tree(name="probe", repo="o/r", ref="master",
                            revision="legacy-content:" + "c" * 64, subdir="",
                            src_root=src, store_root=store, staging_root=staging)
    assert (Path(res.path) / "skills" / "link.md").is_symlink()  # preserved


def test_import_bundle_freezes_files(tmp_path):
    """Sol round-3 H7: imported bundle artifacts are frozen read-only too."""
    import os
    import stat
    src = _plugin_tree(tmp_path)
    bundle, store = tmp_path / "bundle", tmp_path / "store"
    res = publish_from_tree(name="probe", repo="o/r", ref="v1",
                            revision=f"git:{SHA}", subdir="", src_root=src,
                            store_root=bundle, staging_root=tmp_path / "stg")
    import_bundle(bundle, store_root=store)
    skill = store / "probe" / res.artifact_id / "skills" / "s.md"
    assert stat.S_IMODE(os.lstat(skill).st_mode) & 0o222 == 0


def test_publish_rejects_cyclic_symlink(tmp_path):
    """Sol round-4: a symlink LOOP raises unsafe_archive (RuntimeError from
    resolve() translated), not an uncaught error."""
    import os
    src = _plugin_tree(tmp_path)
    os.symlink("b", src / "a")           # a -> b
    os.symlink("a", src / "b")           # b -> a  (cycle)
    store, staging = tmp_path / "store", tmp_path / "staging"
    with pytest.raises(StoreError) as ei:
        publish_from_tree(name="probe", repo="o/r", ref="master",
                          revision="legacy-content:" + "c" * 64, subdir="",
                          src_root=src, store_root=store, staging_root=staging)
    assert ei.value.reason_code == "unsafe_archive"
