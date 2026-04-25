"""Tarball install strategy (§4.3.1).

Security notes (v0.14.6):
- Tar/zip extraction is path-validated and symlink-rejected to prevent
  zip-slip / symlink-escape attacks. ``tarfile.extractall(filter=...)``
  is only available on Python 3.11.4+; production currently runs
  3.11.2 so we validate members manually.
- ``install_cmd`` must be an argv list (not a shell string). Pre-v0.14.6
  it was passed to ``subprocess.run(..., shell=True)``, which let any
  marketplace author execute arbitrary shell as root. Backwards-
  incompatible with any legacy entry that used a string; the first-
  party marketplace ships with no such entry.
- ``url`` must be ``http://``/``https://``. ``file://``/``ftp://``/etc.
  are refused so a poisoned marketplace can't read arbitrary host paths.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
_ALLOWED_INSTALL_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class IntegrityError(RuntimeError):
    """sha256 mismatch — treated as unrecoverable."""


class UnsafeArchiveError(RuntimeError):
    """Refused: archive contains symlink, device, or path-traversal entry."""


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


def _validate_url(url: str) -> None:
    """Refuse non-http(s) schemes — file://, ftp://, jar://, etc."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        raise UnsafeArchiveError(
            f"refusing url with scheme {parsed.scheme!r}; "
            f"allowed: {sorted(_ALLOWED_URL_SCHEMES)}"
        )


def _safe_tar_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract a tarball after validating every member.

    Uses ``filter='data'`` on Python 3.11.4+. Falls back to manual
    validation (no symlinks, no hardlinks, no devices, no FIFOs, no
    absolute paths, no parent-directory traversal) on older Pythons.
    """
    try:
        tf.extractall(dest, filter="data")
        return
    except TypeError:
        pass  # Python <3.11.4 — fall back to manual validation
    except (tarfile.TarError, OSError) as exc:
        raise UnsafeArchiveError(f"tar extract refused by data filter: {exc}") from exc

    target_root = dest.resolve()
    safe_members: list[tarfile.TarInfo] = []
    for m in tf.getmembers():
        if m.issym() or m.islnk():
            raise UnsafeArchiveError(
                f"refusing to extract symlink/hardlink {m.name!r} from tarball"
            )
        if m.isdev() or m.isfifo():
            raise UnsafeArchiveError(
                f"refusing to extract device/fifo {m.name!r} from tarball"
            )
        # An empty/absolute name or one with .. components is rejected by
        # resolved-path containment.
        member_path = (dest / m.name).resolve()
        try:
            member_path.relative_to(target_root)
        except ValueError as exc:
            raise UnsafeArchiveError(
                f"tar member {m.name!r} resolves outside extract_dir"
            ) from exc
        safe_members.append(m)
    tf.extractall(dest, members=safe_members)


def _safe_zip_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a zip archive after validating every member.

    zipfile.extractall sanitises ``..`` and absolute paths, but it still
    extracts symlinks (external_attr high nibble == 0xA) as regular
    files containing the target string on Python <3.12 and as actual
    symlinks on 3.12+. Both behaviours are unsafe — the symlink either
    becomes a confused-deputy bait file or escapes containment outright.
    Reject up front.
    """
    target_root = dest.resolve()
    for info in zf.infolist():
        # Symlinks: high four bits of external_attr's mode == 0xA000.
        mode = (info.external_attr >> 16) & 0xF000
        if mode == 0xA000:
            raise UnsafeArchiveError(
                f"refusing to extract symlink {info.filename!r} from zip"
            )
        member_path = (dest / info.filename).resolve()
        try:
            member_path.relative_to(target_root)
        except ValueError as exc:
            raise UnsafeArchiveError(
                f"zip member {info.filename!r} resolves outside extract_dir"
            ) from exc
    zf.extractall(dest)


def _validate_extract_path(extract_dir: Path, extract: str) -> Path:
    """Resolve `extract` against extract_dir and ensure containment.

    Pre-v0.14.6 a malicious marketplace `extract: "../../../"` escaped
    the temp dir entirely. shutil.copytree on the parent then dragged
    arbitrary host files into install_dir. Containment check closes that.
    """
    if extract == ".":
        return extract_dir
    target_root = extract_dir.resolve()
    candidate = (extract_dir / extract).resolve()
    try:
        candidate.relative_to(target_root)
    except ValueError as exc:
        raise UnsafeArchiveError(
            f"extract path {extract!r} resolves outside the extract dir"
        ) from exc
    return candidate


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

    _validate_url(url)

    tools_root.mkdir(parents=True, exist_ok=True)
    install_dir = tools_root / f"{plugin_name}-{version}"
    tools_bin = tools_root / "bin"
    tools_bin.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "archive"
        urllib.request.urlretrieve(url, archive)  # noqa: S310 (scheme validated above)
        actual = _sha256(archive)
        if actual != expected:
            raise IntegrityError(
                f"sha256 mismatch for {url}: got {actual}, expected {expected}"
            )

        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                _safe_zip_extract(zf, extract_dir)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as tf:
                _safe_tar_extract(tf, extract_dir)
        else:
            raise RuntimeError(f"unsupported archive format: {url}")

        source = _validate_extract_path(extract_dir, extract)
        if install_dir.exists():
            shutil.rmtree(install_dir)
        shutil.copytree(source, install_dir)

        if install_cmd is not None:
            if not isinstance(install_cmd, list) or not all(
                isinstance(a, str) for a in install_cmd
            ):
                raise UnsafeArchiveError(
                    "install_cmd must be a list of strings (argv); "
                    "shell-string form was removed in v0.14.6 to close a "
                    "marketplace-authored shell-injection vector"
                )
            env = {
                "CASA_TOOLS": str(tools_root),
                "PATH": _ALLOWED_INSTALL_PATH,
            }
            subprocess.run(
                install_cmd, shell=False, cwd=install_dir,
                env=env, check=True, timeout=timeout,
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
