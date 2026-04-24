"""Tarball install strategy (§4.3.1)."""
from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class IntegrityError(RuntimeError):
    """sha256 mismatch — treated as unrecoverable."""


@dataclass
class InstallResult:
    ok: bool
    verify_bin_resolves: bool
    install_dir: Path
    message: str = ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def install_tarball(
    *,
    plugin_name: str,
    spec: dict,
    tools_root: Path,
    timeout: int = 120,
) -> InstallResult:
    url = spec["url"]
    expected = spec["sha256"]
    extract = spec.get("extract", ".")
    verify_bin = spec.get("verify_bin")
    install_cmd = spec.get("install_cmd")
    version = spec.get("version", "latest")

    tools_root.mkdir(parents=True, exist_ok=True)
    install_dir = tools_root / f"{plugin_name}-{version}"
    tools_bin = tools_root / "bin"
    tools_bin.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "archive"
        urllib.request.urlretrieve(url, archive)  # noqa: S310 (URL from marketplace, caller validates)
        actual = _sha256(archive)
        if actual != expected:
            raise IntegrityError(
                f"sha256 mismatch for {url}: got {actual}, expected {expected}"
            )

        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract_dir)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as tf:
                tf.extractall(extract_dir)  # noqa: S202
        else:
            raise RuntimeError(f"unsupported archive format: {url}")

        source = extract_dir / extract if extract != "." else extract_dir
        if install_dir.exists():
            shutil.rmtree(install_dir)
        shutil.copytree(source, install_dir)

        if install_cmd:
            env = {"CASA_TOOLS": str(tools_root)}
            subprocess.run(
                install_cmd, shell=True, cwd=install_dir,
                env={**env, "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
                check=True, timeout=timeout,
            )

    # Symlink the verify_bin into tools/bin/.
    resolves = False
    if verify_bin:
        source_bin = None
        for candidate in [install_dir / "bin" / verify_bin, install_dir / verify_bin]:
            if candidate.is_file():
                source_bin = candidate
                break
        if source_bin is None:
            for candidate in install_dir.rglob(verify_bin):
                if candidate.is_file():
                    source_bin = candidate
                    break
        if source_bin is not None:
            link = tools_bin / verify_bin
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(source_bin)
            resolves = link.is_symlink() and link.resolve().is_file()

    return InstallResult(
        ok=True,
        verify_bin_resolves=resolves,
        install_dir=install_dir,
        message="installed via tarball",
    )
