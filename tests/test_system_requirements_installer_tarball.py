"""Tarball install strategy for casa.systemRequirements (§4.3.1)."""
from __future__ import annotations

import hashlib
import http.server
import sys
import tarfile
import threading
from pathlib import Path

import pytest

from system_requirements.tarball import IntegrityError, install_tarball

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
