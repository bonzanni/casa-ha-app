"""Immutable content-addressed plugin artifact store (spec §3.2).

Publish pipeline: resolve ref -> fetch git archive of the exact commit
(bare fetch, never a mutable working clone) -> validate in staging ->
checksum -> metadata INSIDE staging -> atomic rename into the store.
STDLIB-ONLY: imported by the Dockerfile build helper before any venv.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from plugin_registry import STORE_ROOT, compute_artifact_id, normalize_subdir

logger = logging.getLogger(__name__)

STAGING_ROOT = Path("/config/plugins/.staging")
METADATA_FILENAME = ".casa-artifact.json"


class StoreError(Exception):
    reason_code = "store_error"

    def __init__(self, message: str, *, reason_code: str | None = None):
        super().__init__(message)
        if reason_code is not None:
            self.reason_code = reason_code


class RefNotFound(StoreError):
    reason_code = "ref_not_found"


class ResolveUnavailable(StoreError):
    reason_code = "resolve_unavailable"


def _entry_line(rel: str, etype: str, exec_bit: int, payload: str) -> bytes:
    # Length-framed over UTF-8 BYTES (not str chars — multibyte paths would
    # otherwise produce ambiguous frames).
    body = f"{rel}\x00{etype}\x00{exec_bit}\x00{payload}".encode("utf-8")
    return str(len(body)).encode("ascii") + b":" + body


def content_checksum(root: Path) -> str:
    root = Path(root)
    lines: list[bytes] = []
    entries = sorted(
        p for p in root.rglob("*")
        if p.relative_to(root).as_posix() != METADATA_FILENAME
    )
    for p in entries:
        rel = p.relative_to(root).as_posix()
        st = p.lstat()
        exec_bit = 1 if (st.st_mode & stat.S_IXUSR) else 0
        if stat.S_ISLNK(st.st_mode):
            lines.append(_entry_line(rel, "l", 0, os.readlink(p)))
        elif stat.S_ISDIR(st.st_mode):
            lines.append(_entry_line(rel, "d", 0, ""))
        elif stat.S_ISREG(st.st_mode):
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            lines.append(_entry_line(rel, "f", exec_bit, h))
        else:
            raise StoreError(f"special file in artifact: {rel}",
                             reason_code="unsafe_archive")
    return hashlib.sha256(b"".join(lines)).hexdigest()


def safe_extract_tar(tar_path: Path, dest: Path) -> None:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            name = PurePosixPath(m.name)
            if name.is_absolute() or ".." in name.parts:
                raise StoreError(f"unsafe path: {m.name}",
                                 reason_code="unsafe_archive")
            if m.isdev() or m.ischr() or m.isblk() or m.isfifo():
                raise StoreError(f"special file: {m.name}",
                                 reason_code="unsafe_archive")
            if m.issym() or m.islnk():
                target = PurePosixPath(m.linkname)
                if target.is_absolute():
                    raise StoreError(f"absolute link: {m.name}",
                                     reason_code="unsafe_archive")
                joined = (name.parent / target)
                # Normalize and require it stays inside the artifact root.
                parts: list[str] = []
                for part in joined.parts:
                    if part == "..":
                        if not parts:
                            raise StoreError(f"escaping link: {m.name}",
                                             reason_code="unsafe_archive")
                        parts.pop()
                    elif part != ".":
                        parts.append(part)
        tf.extractall(dest, filter="data")


def write_metadata(root: Path, *, name: str, repo: str, ref: str,
                   revision: str, subdir: str, artifact_id: str,
                   version: str, checksum: str) -> None:
    meta = {
        "schema_version": 1,
        "name": name, "repo": repo, "ref": ref, "revision": revision,
        "subdir": normalize_subdir(subdir), "artifact_id": artifact_id,
        "version": version, "content_checksum": checksum,
    }
    p = Path(root) / METADATA_FILENAME
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_metadata(root: Path) -> dict | None:
    try:
        return json.loads((Path(root) / METADATA_FILENAME)
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def validate_artifact(path: Path) -> bool:
    meta = read_metadata(path)
    if not isinstance(meta, dict):
        return False
    try:
        return content_checksum(Path(path)) == meta.get("content_checksum")
    except (OSError, StoreError):
        return False


def artifact_verdict(path: Path, *, name: str, repo: str, revision: str,
                     subdir: str, artifact_id: str) -> str | None:
    """Deep validation against the EXPECTED identity (Sol R2-1).
    None = fully valid; 'artifact_invalid' = metadata/manifest/identity
    problem; 'corrupt_artifact' = identity fine but content checksum fails."""
    meta = read_metadata(path)
    if not isinstance(meta, dict):
        return "artifact_invalid"
    from plugin_registry import normalize_repo as _nrepo
    identity_ok = (
        meta.get("artifact_id") == artifact_id
        and meta.get("name") == name
        and _nrepo(str(meta.get("repo", ""))) == _nrepo(repo)
        and meta.get("revision") == revision
        and meta.get("subdir") == normalize_subdir(subdir)
    )
    if not identity_ok:
        return "artifact_invalid"
    try:
        manifest = json.loads((Path(path) / ".claude-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "artifact_invalid"
    if not isinstance(manifest, dict) or manifest.get("name") != name:
        return "artifact_invalid"
    try:
        if content_checksum(Path(path)) != meta.get("content_checksum"):
            return "corrupt_artifact"
    except (OSError, StoreError):
        return "corrupt_artifact"
    return None
