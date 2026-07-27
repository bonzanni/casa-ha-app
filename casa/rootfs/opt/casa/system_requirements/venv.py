"""venv install strategy (§4.3.1)."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .tarball import InstallResult  # reuse dataclass

logger = logging.getLogger(__name__)


def install_venv(
    *,
    plugin_name: str,
    spec: dict,
    tools_root: Path,
    timeout: int = 300,
) -> InstallResult:
    package = spec["package"]
    python = spec.get("python", "python3")
    verify_bin = spec["verify_bin"]

    venv_dir = tools_root / f"venv-{plugin_name}"
    tools_bin = tools_root / "bin"
    tools_bin.mkdir(parents=True, exist_ok=True)

    # Fresh venv (rollback = rmtree).
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    subprocess.run([python, "-m", "venv", str(venv_dir)],
                   check=True, timeout=timeout)
    subprocess.run(
        [str(venv_dir / "bin" / "pip"), "install", "--no-input", package],
        check=True, timeout=timeout,
    )

    # Symlink verify_bin.
    source_bin = venv_dir / "bin" / verify_bin
    if not source_bin.is_file():
        return InstallResult(ok=False, verify_bin_resolves=False,
                             install_dir=venv_dir,
                             message=f"verify_bin {verify_bin!r} not found in venv")
    link = tools_bin / verify_bin
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(source_bin)

    return InstallResult(
        ok=True,
        verify_bin_resolves=link.is_symlink(),
        install_dir=venv_dir,
        message="installed via venv",
    )
