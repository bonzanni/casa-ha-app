"""npm install strategy (§4.3.1)."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .tarball import InstallResult

logger = logging.getLogger(__name__)


def install_npm(
    *,
    plugin_name: str,
    spec: dict,
    tools_root: Path,
    timeout: int = 180,
) -> InstallResult:
    package = spec["package"]
    verify_bin = spec["verify_bin"]

    npm_root = tools_root / "npm"
    npm_root.mkdir(parents=True, exist_ok=True)
    tools_bin = tools_root / "bin"
    tools_bin.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["npm", "install", "--prefix", str(npm_root), "--no-audit",
         "--no-fund", "--loglevel=error", package],
        check=True, timeout=timeout,
    )

    source_bin = npm_root / "node_modules" / ".bin" / verify_bin
    if not source_bin.is_file():
        return InstallResult(
            ok=False, verify_bin_resolves=False,
            install_dir=npm_root,
            message=f"verify_bin {verify_bin!r} not produced by npm install",
        )
    link = tools_bin / verify_bin
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(source_bin)

    return InstallResult(
        ok=True, verify_bin_resolves=link.is_symlink(),
        install_dir=npm_root, message="installed via npm",
    )
