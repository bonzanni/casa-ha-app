"""Pure s6-rc orchestration helpers. No driver / engagement logic.

All functions here shell out to s6-rc-compile / s6-rc-update / s6-rc /
s6-svstat. Safe to call from both sync and async contexts (functions
labelled async internally use asyncio.to_thread).
"""

from __future__ import annotations

import asyncio
import logging
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
    depends_on: list[str], log_run_script: str | None = None,
) -> str:
    """Create /<svc_root>/engagement-<id>/ with type/run/dependencies.d/.

    When log_run_script is provided, also create a log/ child service dir
    (type=longrun) so s6-log routes stdout to /var/log/casa-engagement-<id>/.

    Returns the full path to the service dir.
    """
    svc_dir = Path(svc_root) / f"engagement-{engagement_id}"
    svc_dir.mkdir(parents=True, exist_ok=False)
    (svc_dir / "type").write_text("longrun\n")
    run_path = svc_dir / "run"
    run_path.write_text(run_script)
    run_path.chmod(run_path.stat().st_mode | stat.S_IXUSR)
    deps_dir = svc_dir / "dependencies.d"
    deps_dir.mkdir()
    for dep in depends_on:
        (deps_dir / dep).touch()

    if log_run_script is not None:
        log_dir = svc_dir / "log"
        log_dir.mkdir()
        (log_dir / "type").write_text("longrun\n")
        log_run_path = log_dir / "run"
        log_run_path.write_text(log_run_script)
        log_run_path.chmod(log_run_path.stat().st_mode | stat.S_IXUSR)
        (log_dir / "dependencies.d").mkdir()
        # Ordering — log dep on the main engagement service so it starts first.
        (log_dir / "dependencies.d" / f"engagement-{engagement_id}").touch()

    return str(svc_dir)


def remove_service_dir(*, svc_root: str, engagement_id: str) -> None:
    """Idempotent rm -rf of /<svc_root>/engagement-<id>/."""
    svc_dir = Path(svc_root) / f"engagement-{engagement_id}"
    if svc_dir.exists():
        shutil.rmtree(svc_dir)


# Module-level lock — guards the full [write-dir → compile → update → change]
# window in driver callers. Callers MUST use `async with _compile_lock:`
# around the full workflow; the helpers below do NOT acquire it themselves.
_compile_lock = asyncio.Lock()


async def _compile_and_update_locked() -> None:
    """Inner helper — caller MUST hold _compile_lock.

    Compiles s6-overlay base + Casa + engagement sources into a fresh
    /tmp/s6-casa-db-<uuid>/, then atomically swaps the live db via
    s6-rc-update.
    """
    new_db = f"/tmp/s6-casa-db-{uuid.uuid4().hex}"
    await asyncio.to_thread(
        subprocess.run,
        [
            "s6-rc-compile",
            new_db,
            S6_OVERLAY_SOURCES,
            CASA_SOURCES,
            ENGAGEMENT_SOURCES_ROOT,
        ],
        check=True,
    )
    await asyncio.to_thread(
        subprocess.run, ["s6-rc-update", new_db], check=True,
    )
    logger.debug("s6-rc live db swapped to %s", new_db)


async def compile_and_update() -> None:
    """Public entry point. Acquires _compile_lock before calling the inner."""
    async with _compile_lock:
        await _compile_and_update_locked()


async def service_pid(*, engagement_id: str) -> int | None:
    """Return the live supervised PID for engagement-<id>, or None if down/absent."""
    result = await asyncio.to_thread(
        subprocess.run,
        ["s6-svstat", "-u", f"/run/service/engagement-{engagement_id}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        pid = int(result.stdout.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


async def start_service(*, engagement_id: str) -> None:
    """s6-rc -u change engagement-<id> — brings the service up. Idempotent."""
    await asyncio.to_thread(
        subprocess.run,
        ["s6-rc", "-u", "change", f"engagement-{engagement_id}"],
        check=True,
    )


async def stop_service(*, engagement_id: str) -> None:
    """s6-rc -d change engagement-<id> — brings the service down. Idempotent."""
    await asyncio.to_thread(
        subprocess.run,
        ["s6-rc", "-d", "change", f"engagement-{engagement_id}"],
        check=True,
    )


def sweep_orphan_service_dirs(
    *, svc_root: str, keep_engagement_ids: set[str],
) -> list[str]:
    """Remove /<svc_root>/engagement-<id>/ where <id> not in keep set.

    Returns the list of removed engagement_ids. Only dirs prefixed with
    'engagement-' are considered — foreign dirs are untouched.
    """
    removed: list[str] = []
    root = Path(svc_root)
    if not root.is_dir():
        return removed
    for entry in root.iterdir():
        if not entry.is_dir() or not entry.name.startswith("engagement-"):
            continue
        eid = entry.name[len("engagement-"):]
        if eid in keep_engagement_ids:
            continue
        logger.warning("s6_rc sweep: removing orphan service dir %s", entry)
        shutil.rmtree(entry)
        removed.append(eid)
    return removed
