"""§3.2 store primitives: content checksum, safe extraction, metadata,
artifact validation. The checksum is length-framed and excludes the
metadata file so metadata can be written INSIDE staging pre-rename."""
from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from plugin_store import (
    METADATA_FILENAME,
    StoreError,
    content_checksum,
    read_metadata,
    safe_extract_tar,
    validate_artifact,
    write_metadata,
)

pytestmark = pytest.mark.unit


def _tree(tmp_path) -> Path:
    root = tmp_path / "art"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "1.0.0"}), encoding="utf-8")
    (root / "skill.md").write_text("hello", encoding="utf-8")
    return root


def test_checksum_stable_and_excludes_metadata(tmp_path):
    root = _tree(tmp_path)
    c1 = content_checksum(root)
    write_metadata(root, name="p", repo="o/r", ref="v1",
                   revision="git:" + "a" * 40, subdir="",
                   artifact_id="0" * 64, version="1.0.0", checksum=c1)
    assert content_checksum(root) == c1          # metadata excluded
    assert validate_artifact(root) is True


def test_checksum_changes_on_content_change(tmp_path):
    root = _tree(tmp_path)
    c1 = content_checksum(root)
    (root / "skill.md").write_text("tampered", encoding="utf-8")
    assert content_checksum(root) != c1


def test_checksum_unicode_paths_framed_by_bytes(tmp_path):
    """Byte-length framing: a multibyte filename must not alias an ASCII
    sibling frame (regression for len(str) vs len(bytes))."""
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text("{}",
                                                             encoding="utf-8")
    (a / "café.md").write_text("x", encoding="utf-8")
    (b / "cafe_.md").write_text("x", encoding="utf-8")
    assert content_checksum(a) != content_checksum(b)


def test_checksum_covers_exec_bit_and_symlink_target(tmp_path):
    root = _tree(tmp_path)
    c1 = content_checksum(root)
    os.chmod(root / "skill.md", 0o755)
    c2 = content_checksum(root)
    assert c2 != c1
    (root / "lnk").symlink_to("skill.md")
    assert content_checksum(root) != c2


def test_validate_artifact_detects_tamper(tmp_path):
    root = _tree(tmp_path)
    write_metadata(root, name="p", repo="o/r", ref="v1",
                   revision="git:" + "a" * 40, subdir="",
                   artifact_id="0" * 64, version="1.0.0",
                   checksum=content_checksum(root))
    (root / "skill.md").write_text("tampered", encoding="utf-8")
    assert validate_artifact(root) is False


def test_metadata_has_no_timestamp(tmp_path):
    root = _tree(tmp_path)
    write_metadata(root, name="p", repo="o/r", ref="v1",
                   revision="git:" + "a" * 40, subdir="",
                   artifact_id="0" * 64, version="1.0.0",
                   checksum=content_checksum(root))
    meta = read_metadata(root)
    assert meta["name"] == "p"
    assert not any("time" in k or k.endswith("_at") for k in meta)


def _tar_bytes(members: list[tuple[str, bytes | None, dict]]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data, extra in members:
            ti = tarfile.TarInfo(name)
            for k, v in extra.items():
                setattr(ti, k, v)
            if data is None:
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return buf


def _write_tar(tmp_path, members) -> Path:
    p = tmp_path / "a.tar"
    p.write_bytes(_tar_bytes(members).getvalue())
    return p


def test_safe_extract_happy(tmp_path):
    tar = _write_tar(tmp_path, [("dir/file.txt", b"ok", {})])
    dest = tmp_path / "out"
    safe_extract_tar(tar, dest)
    assert (dest / "dir" / "file.txt").read_bytes() == b"ok"


def test_safe_extract_falls_back_without_filter_kwarg(tmp_path, monkeypatch):
    """The add-on's base image ships a Python where TarFile.extractall lacks the
    `filter=` kwarg (PEP 706 is 3.12+/3.11.4+). safe_extract_tar must fall back to
    a plain extract — the per-member validation loop is the safety net. (The unit
    gate runs a 3.12 venv, so only the image build exercises this path in CI.)"""
    import tarfile as _tf
    real = _tf.TarFile.extractall

    def _no_filter(self, *args, **kwargs):
        if "filter" in kwargs:
            raise TypeError("extractall() got an unexpected keyword argument 'filter'")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(_tf.TarFile, "extractall", _no_filter)
    tar = _write_tar(tmp_path, [("dir/file.txt", b"ok", {})])
    dest = tmp_path / "out"
    safe_extract_tar(tar, dest)                      # must NOT raise
    assert (dest / "dir" / "file.txt").read_bytes() == b"ok"
    # Unsafe members are still rejected by the validation loop on the fallback path.
    bad = _write_tar(tmp_path, [("../evil", b"x", {})])
    with pytest.raises(StoreError):
        safe_extract_tar(bad, tmp_path / "out2")


@pytest.mark.parametrize("member", [
    ("../evil", b"x", {}),
    ("/abs", b"x", {}),
    ("dev", None, {"type": tarfile.CHRTYPE}),
    ("lnk", None, {"type": tarfile.SYMTYPE, "linkname": "/etc/passwd"}),
    ("lnk2", None, {"type": tarfile.SYMTYPE, "linkname": "../../outside"}),
])
def test_safe_extract_rejects(tmp_path, member):
    """Pins INV-PLUG-003. Red case demonstrated: dropping the `".." in
    name.parts` traversal refusal in safe_extract_tar fails the traversal
    parametrization."""
    tar = _write_tar(tmp_path, [member])
    with pytest.raises(StoreError) as ei:
        safe_extract_tar(tar, tmp_path / "out")
    assert ei.value.reason_code == "unsafe_archive"


def test_safe_extract_allows_relative_inside_symlink(tmp_path):
    tar = _write_tar(tmp_path, [
        ("real.txt", b"x", {}),
        ("lnk", None, {"type": tarfile.SYMTYPE, "linkname": "real.txt"}),
    ])
    dest = tmp_path / "out"
    safe_extract_tar(tar, dest)
    assert (dest / "lnk").is_symlink()


# ---------------------------------------------------------------------------
# #330 — publication crash-durability + validation gaps
# ---------------------------------------------------------------------------


def test_publish_fsyncs_artifact_files_and_directories(tmp_path, monkeypatch):
    """#330: publication used to fsync ONLY the metadata file — after a power
    crash the registry could reference an artifact whose files/dirs never hit
    disk. Every artifact file must be fsynced before the rename, and the
    destination parent directory after it."""
    import stat as stat_mod
    import plugin_store

    file_syncs: list[int] = []
    dir_syncs: list[int] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        if stat_mod.S_ISDIR(os.fstat(fd).st_mode):
            dir_syncs.append(fd)
        else:
            file_syncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(plugin_store.os, "fsync", spy_fsync)

    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "1.0.0"}), encoding="utf-8")
    (root / "skill.md").write_text("hello", encoding="utf-8")

    plugin_store.publish_from_tree(
        name="p", repo="o/r", ref="v1", revision="git:" + "a" * 40,
        subdir="", src_root=root, store_root=tmp_path / "store",
        staging_root=tmp_path / "staging")

    # plugin.json + skill.md + metadata — not just the metadata file.
    assert len(file_syncs) >= 3, "artifact files were not fsynced"
    assert dir_syncs, "no directory fsync — the rename is not crash-durable"


def test_artifact_verdict_rechecks_setup_tool(tmp_path):
    """#330: artifact_verdict never re-checked casa.setupTool — a
    pre-validator artifact with an invalid declaration passed snapshot
    validation and loaded, silently skipping automatic setup. Same
    upgrade-path posture as protectedTools/triggers."""
    import plugin_store

    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "1.0.0"}), encoding="utf-8")
    res = plugin_store.publish_from_tree(
        name="p", repo="o/r", ref="v1", revision="git:" + "a" * 40,
        subdir="", src_root=root, store_root=tmp_path / "store",
        staging_root=tmp_path / "staging")

    # Simulate a pre-v0.112.0 artifact: rewrite plugin.json with an invalid
    # setupTool and re-align the stored checksum (identity/content stay
    # consistent — only the setupTool gate can catch it).
    art = Path(res.path)
    pj = art / ".claude-plugin" / "plugin.json"
    os.chmod(art, 0o755)
    os.chmod(art / ".claude-plugin", 0o755)
    os.chmod(pj, 0o644)
    pj.write_text(json.dumps({
        "name": "p", "version": "1.0.0",
        "casa": {"setupTool": "not_setup_prefixed"},
    }), encoding="utf-8")
    meta_path = art / METADATA_FILENAME
    os.chmod(meta_path, 0o644)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["content_checksum"] = content_checksum(art)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True),
                         encoding="utf-8")

    verdict = plugin_store.artifact_verdict(
        art, name="p", repo="o/r", revision="git:" + "a" * 40,
        subdir="", artifact_id=res.artifact_id)
    assert verdict == "setup_tool_invalid"


def test_parse_mcp_servers_rejects_non_mapping_env(tmp_path):
    """#330: ``"env": []`` used to pass validation and later crash
    ``extract_env_vars`` (``env.values()`` on a list) — a declared env must
    be a mapping or the server is not runnable (malformed)."""
    import plugin_store

    _, malformed = plugin_store.parse_mcp_servers_text(json.dumps({
        "mcpServers": {"s": {"command": "npx", "env": ["OOPS"]}},
    }))
    assert malformed is True


def test_extract_env_vars_tolerates_non_mapping_env(tmp_path):
    """#330: the wrapper-form servers map still carries a bad-env entry for
    grant derivation — extraction must skip it, not AttributeError-abort
    specialist repo inspection."""
    from plugin_env_extractor import extract_env_vars

    p = tmp_path / ".mcp.json"
    p.write_text(json.dumps({
        "mcpServers": {
            "bad": {"command": "npx", "env": ["OOPS"]},
            "good": {"command": "npx", "env": {"K": "${MY_SECRET}"}},
        },
    }), encoding="utf-8")
    assert extract_env_vars(p) == {"MY_SECRET"}


def test_publish_fsyncs_store_root_for_new_plugin_dir(tmp_path, monkeypatch):
    """Sol r1 (#330): fsyncing store_root/<name>/ does not make <name>'s OWN
    entry in store_root durable — a first-time publication must fsync the
    store root too, or a power crash can lose the whole plugin-name directory
    while the registry survives referencing it."""
    import stat as stat_mod
    import plugin_store

    dir_inodes_synced: set[tuple[int, int]] = set()
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        st = os.fstat(fd)
        if stat_mod.S_ISDIR(st.st_mode):
            dir_inodes_synced.add((st.st_dev, st.st_ino))
        real_fsync(fd)

    monkeypatch.setattr(plugin_store.os, "fsync", spy_fsync)

    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "1.0.0"}), encoding="utf-8")
    store_root = tmp_path / "store"

    plugin_store.publish_from_tree(
        name="p", repo="o/r", ref="v1", revision="git:" + "a" * 40,
        subdir="", src_root=root, store_root=store_root,
        staging_root=tmp_path / "staging")

    st = os.stat(store_root)
    assert (st.st_dev, st.st_ino) in dir_inodes_synced, (
        "store_root itself was never fsynced — the new plugin-name entry is "
        "not durable"
    )


def test_publish_fails_when_directory_fsync_fails(tmp_path, monkeypatch):
    """Terra r2 (#330): the publication durability barrier must be STRICT —
    a failed directory fsync silently reported success, so a registry write
    could survive a power loss while the artifact tree's directory entries
    did not. Nothing references the artifact yet, so failing loudly is
    safe."""
    import stat as stat_mod
    import plugin_store

    real_fsync = os.fsync

    def failing_dir_fsync(fd: int) -> None:
        if stat_mod.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated dir fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(plugin_store.os, "fsync", failing_dir_fsync)

    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "1.0.0"}), encoding="utf-8")

    with pytest.raises(Exception):
        plugin_store.publish_from_tree(
            name="p", repo="o/r", ref="v1", revision="git:" + "a" * 40,
            subdir="", src_root=root, store_root=tmp_path / "store",
            staging_root=tmp_path / "staging")


def test_existing_destination_republish_enforces_dir_barrier(
    tmp_path, monkeypatch,
):
    """Sol r3 (#330): a strict-fsync failure AFTER the rename leaves dest
    installed; the idempotent dest.exists() re-publish then returned success
    without ever completing the directory barriers — the registry reference
    could still outlive the artifact across a power crash. The idempotent
    path must re-run the barriers."""
    import stat as stat_mod
    import plugin_store

    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "1.0.0"}), encoding="utf-8")

    kwargs = dict(
        name="p", repo="o/r", ref="v1", revision="git:" + "a" * 40,
        subdir="", src_root=root, store_root=tmp_path / "store",
        staging_root=tmp_path / "staging")
    plugin_store.publish_from_tree(**kwargs)     # dest now installed

    real_fsync = os.fsync

    def failing_dir_fsync(fd: int) -> None:
        if stat_mod.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated dir fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(plugin_store.os, "fsync", failing_dir_fsync)
    with pytest.raises(Exception):
        plugin_store.publish_from_tree(**kwargs)
