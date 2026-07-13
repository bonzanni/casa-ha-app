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
