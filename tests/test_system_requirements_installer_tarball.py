"""Tarball install strategy for casa.systemRequirements (§4.3.1).

Includes Bug 2 / Bug 3 (v0.14.6) regression suite:
- Symlinked tar member is rejected with UnsafeArchiveError.
- Path-traversal tar member (../../etc/foo) is rejected.
- Zip member with absolute path / `..` is rejected.
- Zip member encoded as a symlink (external_attr 0xA000) is rejected.
- Bad URL scheme (file://, ftp://) is refused before download.
- `extract` field with `..` is refused.
- `install_cmd` as a string raises (argv-list-only).
"""
from __future__ import annotations

import hashlib
import http.server
import io
import os
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

from system_requirements.tarball import (
    IntegrityError, UnsafeArchiveError, install_tarball,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def fixture_tarball(tmp_path: Path) -> tuple[Path, str]:
    """Build a small fixture tarball containing bin/fakebin (a shell stub)."""
    pkg_dir = tmp_path / "pkg"
    (pkg_dir / "bin").mkdir(parents=True)
    (pkg_dir / "bin" / "fakebin").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    (pkg_dir / "bin" / "fakebin").chmod(0o755)

    tar_path = tmp_path / "fakebin.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(pkg_dir, arcname=".")
    sha = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    return tar_path, sha


@pytest.fixture
def http_server(fixture_tarball, tmp_path: Path):
    tar_path, sha = fixture_tarball

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:  # type: ignore[override]
            return str(tar_path)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/pkg.tar.gz", sha
    server.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink requires developer mode on Windows")
def test_install_happy(tmp_path: Path, http_server) -> None:
    url, sha = http_server
    result = install_tarball(
        plugin_name="fakebin",
        spec={
            "type": "tarball",
            "url": url,
            "sha256": sha,
            "extract": ".",
            "verify_bin": "fakebin",
        },
        tools_root=tmp_path / "tools",
    )
    assert result.ok
    assert result.verify_bin_resolves
    assert (tmp_path / "tools" / "bin" / "fakebin").is_symlink()


def test_integrity_mismatch(tmp_path: Path, http_server) -> None:
    url, _sha = http_server
    with pytest.raises(IntegrityError):
        install_tarball(
            plugin_name="fakebin",
            spec={
                "type": "tarball",
                "url": url,
                "sha256": "0" * 64,
                "extract": ".",
                "verify_bin": "fakebin",
            },
            tools_root=tmp_path / "tools",
        )
    # Clean rollback: tools dir empty.
    assert list((tmp_path / "tools").rglob("*")) == [tmp_path / "tools" / "bin"] or \
           list((tmp_path / "tools").rglob("*")) == []


# ---------------------------------------------------------------------------
# Bug 2 / Bug 3 (v0.14.6) regression suite
# ---------------------------------------------------------------------------


def _serve_local_file(path: Path):
    """Serve `path` over a throwaway HTTP server. Returns (url, sha)."""
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, p: str) -> str:  # type: ignore[override]
            return str(path)

        def log_message(self, *args, **kwargs) -> None:  # silence
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/x", sha, server


@pytest.mark.skipif(sys.platform == "win32",
                    reason="symlink in tarball requires posix")
def test_symlink_member_rejected(tmp_path: Path) -> None:
    """A tarball with a symlink pointing at /etc must be refused.

    Pre-fix: tarfile.extractall extracted the symlink as written;
    later steps could traverse it to host files.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "real.txt").write_text("ok", encoding="utf-8")
    os.symlink("/etc", pkg / "escape")

    tar_path = tmp_path / "bad.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(pkg, arcname=".")
    url, sha, server = _serve_local_file(tar_path)
    try:
        # Accept either our explicit "symlink" wording (raised by the
        # member-iteration guard at tarball.py:89) OR Python 3.11+'s
        # tarfile.AbsoluteLinkError "link to an absolute path" wording
        # (raised by the data filter at tarball.py:83 before we ever see
        # the member). Both signal correct refusal of the unsafe entry.
        with pytest.raises(UnsafeArchiveError,
                           match=r"symlink|link to an absolute path"):
            install_tarball(
                plugin_name="evil",
                spec={"type": "tarball", "url": url, "sha256": sha,
                      "extract": ".", "verify_bin": "real.txt"},
                tools_root=tmp_path / "tools",
            )
    finally:
        server.shutdown()


def test_path_traversal_member_rejected(tmp_path: Path) -> None:
    """A tar member named ../escape.txt is refused before extraction."""
    tar_path = tmp_path / "bad.tar.gz"
    payload = io.BytesIO(b"pwned")
    with tarfile.open(tar_path, "w:gz") as tf:
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(payload.getvalue())
        payload.seek(0)
        tf.addfile(info, payload)

    url, sha, server = _serve_local_file(tar_path)
    try:
        with pytest.raises(UnsafeArchiveError):
            install_tarball(
                plugin_name="evil",
                spec={"type": "tarball", "url": url, "sha256": sha,
                      "extract": ".", "verify_bin": "x"},
                tools_root=tmp_path / "tools",
            )
        # And nothing escaped to the parent of tools_root.
        assert not (tmp_path / "escape.txt").exists()
    finally:
        server.shutdown()


def test_zip_path_traversal_rejected(tmp_path: Path) -> None:
    """A zip with a member resolving above the extract dir is refused."""
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../escape.txt", b"pwned")
    url, sha, server = _serve_local_file(zip_path)
    try:
        with pytest.raises(UnsafeArchiveError):
            install_tarball(
                plugin_name="evil",
                spec={"type": "tarball", "url": url, "sha256": sha,
                      "extract": ".", "verify_bin": "x"},
                tools_root=tmp_path / "tools",
            )
        assert not (tmp_path / "escape.txt").exists()
    finally:
        server.shutdown()


def test_zip_symlink_member_rejected(tmp_path: Path) -> None:
    """Zip member with external_attr indicating a symlink is refused."""
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        info = zipfile.ZipInfo("escape")
        # 0xA000 = symlink mode; <<16 places it in the high word of external_attr.
        info.external_attr = (0xA1FF << 16) | 0
        zf.writestr(info, b"/etc")
    url, sha, server = _serve_local_file(zip_path)
    try:
        with pytest.raises(UnsafeArchiveError, match="symlink"):
            install_tarball(
                plugin_name="evil",
                spec={"type": "tarball", "url": url, "sha256": sha,
                      "extract": ".", "verify_bin": "x"},
                tools_root=tmp_path / "tools",
            )
    finally:
        server.shutdown()


def test_extract_path_traversal_refused(tmp_path: Path, fixture_tarball) -> None:
    """spec.extract='../..' must be refused even with a clean tarball."""
    tar_path, sha = fixture_tarball
    url, _sha2, server = _serve_local_file(tar_path)
    try:
        with pytest.raises(UnsafeArchiveError, match="extract path"):
            install_tarball(
                plugin_name="fakebin",
                spec={"type": "tarball", "url": url, "sha256": sha,
                      "extract": "../..", "verify_bin": "fakebin"},
                tools_root=tmp_path / "tools",
            )
    finally:
        server.shutdown()


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://anonymous@example.com/x.tgz",
    "jar:http://x/y.jar!/inside",
])
def test_unsafe_url_schemes_refused(tmp_path: Path, url: str) -> None:
    with pytest.raises(UnsafeArchiveError, match="scheme"):
        install_tarball(
            plugin_name="x",
            spec={"type": "tarball", "url": url, "sha256": "0" * 64,
                  "extract": ".", "verify_bin": "x"},
            tools_root=tmp_path / "tools",
        )


def test_install_cmd_string_refused(tmp_path: Path, fixture_tarball) -> None:
    """install_cmd as a shell string is no longer accepted.

    Pre-v0.14.6: subprocess.run(install_cmd, shell=True) — full RCE on
    the host as root for any marketplace author. The fix accepts only
    a list[str] (argv).
    """
    tar_path, sha = fixture_tarball
    url, _sha, server = _serve_local_file(tar_path)
    try:
        with pytest.raises(UnsafeArchiveError, match="install_cmd"):
            install_tarball(
                plugin_name="fakebin",
                spec={
                    "type": "tarball", "url": url, "sha256": sha,
                    "extract": ".", "verify_bin": "fakebin",
                    "install_cmd": "echo 'this used to be shell-eval'",
                },
                tools_root=tmp_path / "tools",
            )
    finally:
        server.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell needed for echo argv")
def test_install_cmd_argv_list_runs(tmp_path: Path, fixture_tarball) -> None:
    """install_cmd as argv list runs without shell=True."""
    tar_path, sha = fixture_tarball
    url, _sha, server = _serve_local_file(tar_path)
    try:
        result = install_tarball(
            plugin_name="fakebin",
            spec={
                "type": "tarball", "url": url, "sha256": sha,
                "extract": ".", "verify_bin": "fakebin",
                "install_cmd": ["true"],   # benign argv
            },
            tools_root=tmp_path / "tools",
        )
        assert result.ok
    finally:
        server.shutdown()
