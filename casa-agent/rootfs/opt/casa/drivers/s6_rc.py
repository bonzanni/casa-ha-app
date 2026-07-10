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

# v0.64.0: every engagement service has a sibling logger service. The
# suffix/name convention is owned HERE — no caller formats these names.
LOG_SERVICE_SUFFIX = "-log"


def _main_service_name(engagement_id: str) -> str:
    return f"engagement-{engagement_id}"


def _log_service_name(engagement_id: str) -> str:
    return f"engagement-{engagement_id}{LOG_SERVICE_SUFFIX}"


def _emit_longrun(
    svc_dir: Path, *, run_script: str, depends_on: list[str],
) -> None:
    """Write one longrun source-definition dir (type/run/dependencies.d)."""
    svc_dir.mkdir(parents=True, exist_ok=False)
    (svc_dir / "type").write_text("longrun\n")
    run_path = svc_dir / "run"
    run_path.write_text(run_script)
    run_path.chmod(run_path.stat().st_mode | stat.S_IXUSR)
    deps_dir = svc_dir / "dependencies.d"
    deps_dir.mkdir()
    for dep in depends_on:
        (deps_dir / dep).touch()


def write_service_dir(
    *, svc_root: str, engagement_id: str, run_script: str,
    depends_on: list[str], log_run_script: str | None = None,
) -> str:
    """Create /<svc_root>/engagement-<id>/ with type/run/dependencies.d/.

    When log_run_script is provided, also create a SIBLING top-level
    service dir ``engagement-<id>-log`` wired to the main service via
    ``producer-for``/``consumer-for``, so s6-rc-compile builds a
    producer-consumer pipeline and s6-log routes the CLI's stdout to
    the engagement's log dir. A nested ``log/`` subdir is s6-scandir
    convention that s6-rc-compile explicitly ignores — using it left the
    subprocess stdout on a reader-less pipe from v0.13.0 through v0.63.x
    (P31's deeper cause). s6-rc auto-adds the producer→consumer dependency
    and holds the pipe in s6rc-fdholder, so log lines survive producer
    respawns (verified on s6-rc 0.6.0.0, 2026-07-10 design doc).

    The cross-reference (``producer-for``) is written LAST: until then both
    dirs compile standalone, so a crash mid-write cannot leave the sources
    un-compilable (and ``_prune_broken_pairs`` clears any residue anyway).

    Returns the full path to the main service dir.
    """
    svc_dir = Path(svc_root) / _main_service_name(engagement_id)
    _emit_longrun(svc_dir, run_script=run_script, depends_on=depends_on)

    if log_run_script is not None:
        log_name = _log_service_name(engagement_id)
        log_dir = Path(svc_root) / log_name
        _emit_longrun(log_dir, run_script=log_run_script, depends_on=depends_on)
        (log_dir / "consumer-for").write_text(
            _main_service_name(engagement_id) + "\n")
        (svc_dir / "producer-for").write_text(log_name + "\n")

    return str(svc_dir)


def remove_service_dir(*, svc_root: str, engagement_id: str) -> None:
    """Best-effort idempotent removal of the engagement's service pair.

    Logger first: if the main rmtree then fails, the survivor's dangling
    ``producer-for`` is cleared by ``_prune_broken_pairs`` on the next
    compile, so a partial removal can never wedge the sources. Failures are
    warned, not raised — callers treat removal as best-effort teardown, and
    the recompile that follows drops removed services from the live db,
    which stops them (s6-rc-update semantics).
    """
    for name in (_log_service_name(engagement_id),
                 _main_service_name(engagement_id)):
        svc_dir = Path(svc_root) / name
        if svc_dir.exists():
            try:
                shutil.rmtree(svc_dir)
            except OSError as exc:
                logger.warning(
                    "remove_service_dir: rmtree %s failed: %s", svc_dir, exc,
                )


def service_pair_complete(*, svc_root: str, engagement_id: str) -> bool:
    """True iff the v0.64.0 service pair is fully present: main dir +
    ``producer-for`` + ``-log`` sibling. Legacy (≤v0.63.x) dirs and torn
    halves return False so boot replay re-plants (and thereby migrates)
    them."""
    root = Path(svc_root)
    main = root / _main_service_name(engagement_id)
    return (
        main.is_dir()
        and (main / "producer-for").is_file()
        and (root / _log_service_name(engagement_id)).is_dir()
    )


def _prune_broken_pairs(*, svc_root: str) -> list[str]:
    """Make the engagement sources compilable again after a crash.

    The pair dirs cross-reference each other, so no write/remove ordering
    is crash-atomic — and a torn half (``producer-for`` naming a missing
    service, or a logger whose producer no longer declares it) fails the
    WHOLE s6-rc-compile, bricking every engagement start/stop. Rules:

      - main dir whose ``producer-for`` names a missing sibling → unlink
        ``producer-for`` (the service survives, unlogged);
      - ``-log`` dir whose main lacks a ``producer-for`` (or is gone) →
        remove the ``-log`` dir (a logger alone cannot compile).

    Engagement ids are hex UUIDs, so the suffix is unambiguous. Returns the
    pruned paths. Called under ``_compile_lock`` before every compile.
    """
    pruned: list[str] = []
    root = Path(svc_root)
    if not root.is_dir():
        return pruned
    for entry in root.iterdir():
        if not entry.is_dir() or not entry.name.startswith("engagement-"):
            continue
        if entry.name.endswith(LOG_SERVICE_SUFFIX):
            main = root / entry.name[:-len(LOG_SERVICE_SUFFIX)]
            if not (main / "producer-for").is_file():
                logger.warning(
                    "s6_rc prune: removing torn logger dir %s", entry,
                )
                shutil.rmtree(entry, ignore_errors=True)
                pruned.append(str(entry))
        else:
            producer_for = entry / "producer-for"
            if producer_for.is_file():
                target = producer_for.read_text().strip()
                if target and not (root / target).is_dir():
                    logger.warning(
                        "s6_rc prune: unlinking dangling producer-for in %s",
                        entry,
                    )
                    producer_for.unlink(missing_ok=True)
                    pruned.append(str(producer_for))
    return pruned


# Module-level lock — guards the full [write-dir → compile → update → change]
# window in driver callers. Callers MUST use `async with _compile_lock:`
# around the full workflow; the helpers below do NOT acquire it themselves.
_compile_lock = asyncio.Lock()


async def _compile_and_update_locked() -> None:
    """Inner helper — caller MUST hold _compile_lock.

    Compiles s6-overlay base + Casa + engagement sources into a fresh
    /tmp/s6-casa-db-<uuid>/, then atomically swaps the live db via
    s6-rc-update. Reaps the previously-live compiled db after a successful
    swap (or the just-compiled db after a failed one) so /tmp doesn't
    accumulate one orphaned db per compile (L12 leak guard).
    """
    # v0.64.0: clear any crash-torn producer/consumer halves first — a
    # single dangling cross-reference fails the whole compile.
    _prune_broken_pairs(svc_root=ENGAGEMENT_SOURCES_ROOT)

    old_db = os.path.realpath(LIVE_DB_SYMLINK)
    new_db = f"/tmp/s6-casa-db-{uuid.uuid4().hex}"
    try:
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
    except BaseException:
        # Failed swap: the fresh compile is the orphan.
        shutil.rmtree(new_db, ignore_errors=True)
        raise
    # Successful swap: the previous db is unused now. Only reap dirs we
    # created (basename prefix guard keeps a foreign/boot db in /run safe).
    if old_db != new_db and os.path.basename(old_db).startswith("s6-casa-db-"):
        shutil.rmtree(old_db, ignore_errors=True)
    logger.debug("s6-rc live db swapped to %s", new_db)


async def compile_and_update() -> None:
    """Public entry point. Acquires _compile_lock before calling the inner."""
    async with _compile_lock:
        await _compile_and_update_locked()


async def service_pid(*, engagement_id: str) -> int | None:
    """Return the live supervised PID for engagement-<id>, or None if down/absent.

    Uses ``s6-svstat -p`` which prints the supervised process PID
    (or ``0`` when the service is down). The earlier flag ``-u`` printed
    ``true``/``false`` (up status) which always failed ``int()`` and
    silently returned ``None`` — every engagement looked dead, breaking
    is_alive_async on every restart-survival code path.
    """
    result = await asyncio.to_thread(
        subprocess.run,
        ["s6-svstat", "-p", f"/run/service/engagement-{engagement_id}"],
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


async def stop_log_service(*, engagement_id: str) -> None:
    """Down the engagement's sibling logger service, if it has one.

    No-op when the ``-log`` source dir is absent (legacy pre-v0.64.0
    engagements) so callers never exec a doomed s6-rc or log spurious
    warnings. Keeps the suffix convention inside this module.
    """
    log_dir = Path(ENGAGEMENT_SOURCES_ROOT) / _log_service_name(engagement_id)
    if not log_dir.is_dir():
        return
    await stop_service(engagement_id=f"{engagement_id}{LOG_SERVICE_SUFFIX}")


def sweep_orphan_compiled_dbs(*, tmp_root: str = "/tmp") -> list[str]:
    """Remove stale /tmp/s6-casa-db-* dirs left by previous container runs.

    Skips the current live db (target of LIVE_DB_SYMLINK). Returns removed
    paths. Called once at boot replay — after a container restart /run is
    fresh tmpfs, so every leftover /tmp/s6-casa-db-* from the prior run is
    stale and safe to reap (L12 leak guard).
    """
    live = os.path.realpath(LIVE_DB_SYMLINK)
    removed: list[str] = []
    root = Path(tmp_root)
    if not root.is_dir():
        return removed
    for entry in root.iterdir():
        if not entry.is_dir() or not entry.name.startswith("s6-casa-db-"):
            continue
        if os.path.realpath(entry) == live:
            continue
        logger.warning("s6_rc sweep: removing stale compiled db %s", entry)
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(str(entry))
    return removed


def sweep_orphan_service_dirs(
    *, svc_root: str, keep_engagement_ids: set[str],
) -> list[str]:
    """Remove /<svc_root>/engagement-<id>[-log]/ where <id> not in keep set.

    Returns the list of removed engagement_ids (an orphan main+logger pair
    counts once). Only dirs prefixed with 'engagement-' are considered —
    foreign dirs are untouched. Logger siblings (``engagement-<id>-log``)
    live and die with their engagement; engagement ids are hex UUIDs, so
    the ``-log`` suffix is unambiguous.
    """
    removed: list[str] = []
    root = Path(svc_root)
    if not root.is_dir():
        return removed
    for entry in root.iterdir():
        if not entry.is_dir() or not entry.name.startswith("engagement-"):
            continue
        eid = entry.name[len("engagement-"):].removesuffix(LOG_SERVICE_SUFFIX)
        if eid in keep_engagement_ids:
            continue
        logger.warning("s6_rc sweep: removing orphan service dir %s", entry)
        shutil.rmtree(entry)
        if eid not in removed:
            removed.append(eid)
    return removed
