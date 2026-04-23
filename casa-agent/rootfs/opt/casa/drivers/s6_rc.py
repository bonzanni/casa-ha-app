"""Pure s6-rc orchestration helpers. No driver / engagement logic.

All functions here shell out to s6-rc-compile / s6-rc-update / s6-rc /
s6-svstat. Safe to call from both sync and async contexts (functions
labelled async internally use asyncio.to_thread).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Constants — can be overridden in tests via monkeypatch.
S6_OVERLAY_SOURCES = "/package/admin/s6-overlay-3.2.2.0/etc/s6-rc/sources"
CASA_SOURCES = "/etc/s6-overlay/s6-rc.d"
ENGAGEMENT_SOURCES_ROOT = "/data/casa-s6-services"
LIVE_DB_SYMLINK = "/run/s6-rc/compiled"


def write_service_dir(
    *, svc_root: str, engagement_id: str, run_script: str,
    depends_on: list[str],
) -> str:
    """Create /<svc_root>/engagement-<id>/ with type/run/dependencies.d/.

    Returns the full path to the service dir.
    """
    svc_dir = Path(svc_root) / f"engagement-{engagement_id}"
    svc_dir.mkdir(parents=True, exist_ok=False)
    (svc_dir / "type").write_text("longrun\n")
    run_path = svc_dir / "run"
    run_path.write_text(run_script)
    run_path.chmod(run_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    deps_dir = svc_dir / "dependencies.d"
    deps_dir.mkdir()
    for dep in depends_on:
        (deps_dir / dep).touch()
    return str(svc_dir)


def remove_service_dir(*, svc_root: str, engagement_id: str) -> None:
    """Idempotent rm -rf of /<svc_root>/engagement-<id>/."""
    svc_dir = Path(svc_root) / f"engagement-{engagement_id}"
    if svc_dir.exists():
        shutil.rmtree(svc_dir)
