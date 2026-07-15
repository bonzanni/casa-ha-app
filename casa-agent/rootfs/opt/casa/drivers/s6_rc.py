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
import signal
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

# The live s6 scandir root where s6-svscan supervises each running service.
# Split out as a constant so the checked-teardown ladder can be tested with a
# tmp scandir root (monkeypatch), same pattern as ENGAGEMENT_SOURCES_ROOT.
SERVICE_SCANDIR_ROOT = "/run/service"

# Short wait between checked-teardown attempts (overridable in tests so the
# ladder retries without a real sleep). NOT a patch of asyncio.sleep — the
# memory-cage rule forbids patching <module>.asyncio.sleep globally.
_ENSURE_DOWN_WAIT_S = 0.2

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


# A run script is "current" (v0.75.0+) iff it carries BOTH streaming markers:
# the pre-exec ``casa_control`` spawn NDJSON frame AND the ``--output-format
# stream-json`` CLI flag. v0.75 message-granularity streaming needs the two
# together — the spawn frame arms the driver's _InboundSpool and the CLI flag
# makes the process actually emit the NDJSON the relay consumes. A script with
# only one is half-wired and still stale; a pre-v0.75 script has neither. Boot
# replay uses this to migrate stale pairs.
_CURRENT_RUN_MARKERS = ("casa_control", "--output-format stream-json")


def run_script_is_stale(*, svc_root: str, engagement_id: str) -> bool:
    """True iff the persisted MAIN run script is NOT the v0.75.0 streaming
    contract — i.e. it does not carry BOTH ``casa_control`` AND
    ``--output-format stream-json``.

    Fails CLOSED (stale=True) when the run file is missing or unreadable: a
    resumed pair we cannot prove is current must be re-planted rather than
    started on a possibly-unarmed script (``service_pair_complete`` does NOT
    inspect the run file, so this predicate is the only gate that does)."""
    run_path = Path(svc_root) / _main_service_name(engagement_id) / "run"
    try:
        text = run_path.read_text()
    except OSError:
        return True
    return not all(marker in text for marker in _CURRENT_RUN_MARKERS)


def service_dirs_absent(*, svc_root: str, engagement_id: str) -> bool:
    """True iff BOTH the main and ``-log`` service source dirs are gone.

    Boot replay's stale-pair migration calls this AFTER ``remove_service_dir``
    to VERIFY the removal actually happened before re-planting.
    ``remove_service_dir`` swallows rmtree failures, so a surviving old main —
    or a partial removal (log gone, main survives) — must read as NOT absent
    (fail closed): a survivor would collide with ``write_service_dir``'s
    ``exist_ok=False`` re-plant and leave a stale, unlogged main whose spawn
    frames never reach the relay."""
    root = Path(svc_root)
    main = root / _main_service_name(engagement_id)
    log = root / _log_service_name(engagement_id)
    return not main.exists() and not log.exists()


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


def _service_scandir(engagement_id: str) -> str:
    return os.path.join(SERVICE_SCANDIR_ROOT, _main_service_name(engagement_id))


async def _probe_service_down(scandir: str) -> str:
    """The ONE strict status probe used EVERYWHERE in the teardown ladder
    (r16-B1 — there is NO ``-o up``-only probe anywhere). Returns exactly one
    of ``"down"`` / ``"up"`` / ``"unknown"``:

      - scandir ABSENT → ``"down"``.
      - ``s6-svstat -o up,wantedup`` == ``"false false"`` → ``"down"`` (process
        down AND down-intent latched — a respawning supervisor cannot revive
        it).
      - ``"false true"`` → ``"up"`` — transiently dead but wanted-up, s6 WILL
        respawn it; NOT down (explicitly, the respawn-race case).
      - ``"true *"`` → ``"up"``.
      - non-zero rc / unparseable output WITH the scandir present → ``"unknown"``
        (a query failure is NOT proof of down; the ladder retries).

    ``service_pid`` returning None is deliberately NOT used here: it maps
    ``s6-svstat`` errors AND malformed output to None, so None is not strict
    proof of down.
    """
    if not os.path.isdir(scandir):
        return "down"
    result = await asyncio.to_thread(
        subprocess.run,
        ["s6-svstat", "-o", "up,wantedup", scandir],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return "unknown"
    fields = result.stdout.split()
    if len(fields) != 2:
        return "unknown"
    up, wantedup = fields[0], fields[1]
    if up == "false" and wantedup == "false":
        return "down"
    if up == "false" and wantedup == "true":
        return "up"        # respawn-race: wanted up, will be revived → NOT down
    if up == "true":
        return "up"
    return "unknown"


async def _direct_killpg(scandir: str) -> bool:
    """Last-resort SIGKILL of the supervised leader's whole PROCESS GROUP.

    Used only AFTER a durable ``s6-svc -D`` down-latch when the supervisor is
    unresponsive. Reads the leader PID from ``s6-svstat -p``, SIGKILLs its
    process group (``os.killpg`` — so Claude's MCP/tool subprocesses die with
    the leader, not orphaned), falling back to ``os.kill`` only when the leader
    is not itself a group leader. Returns True iff a kill signal was delivered
    (False on a kernel-refused / vanished PID — that keeps a genuine
    ``refuse_teardown_failed`` reachable)."""
    result = await asyncio.to_thread(
        subprocess.run,
        ["s6-svstat", "-p", scandir],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    try:
        pid = int(result.stdout.strip())
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    try:
        if pgid is not None and pgid == pid:
            os.killpg(pgid, signal.SIGKILL)
        else:
            # Leader is not a group leader — kill it directly (r14 fallback).
            os.kill(pid, signal.SIGKILL)
        return True
    except OSError as exc:
        logger.warning(
            "ensure_service_down: direct SIGKILL of pid %s failed: %s",
            pid, exc,
        )
        return False


async def ensure_service_down(*, engagement_id: str, attempts: int = 3) -> bool:
    """Checked teardown ladder for a refused/torn engagement (r13-B1).

    ``stop_service`` (``s6-rc -d change``) uses ``check=True`` so its own raise
    would fall into replay's warn-and-continue, and a still-up PID was
    previously only logged — this helper turns teardown into a CHECKED result.
    Per attempt:

      1. ``s6-rc -d change engagement-<id>`` with CalledProcessError CAUGHT (a
         failed s6-rc is an INPUT to the ladder, not an escape from it).
      2. the strict ``_probe_service_down`` probe → ``"down"`` ⇒ return True.
      3. still up/unknown → direct supervisor fallback ``s6-svc -D <scandir>``
         (capital ``-D`` also writes ``./down``, so the down survives an
         ``s6-supervise`` restart) → re-probe (short wait first).
      4. on the LAST attempt, the PHYSICAL-CONTAINMENT rung: the COMBINED
         ``s6-svc -wD -KD -T 5000 <scandir>`` — ``-D`` durably latches down,
         ``-K`` SIGKILLs the whole process GROUP, ``-wD`` blocks until really
         down, ``-T`` bounds the wait. If the supervisor is unresponsive, a
         direct ``os.killpg`` fallback follows (only after the ``-D`` latch).

    Returns True ONLY on the strict ``false false`` / scandir-absent
    confirmation. False stays reachable via a kernel-refused SIGKILL OR a
    persistent status-query failure — the caller lands a terminal
    ``mark_error(kind="refuse_teardown_failed")`` + operator-visible ERROR.
    """
    scandir = _service_scandir(engagement_id)
    main = _main_service_name(engagement_id)

    for attempt in range(attempts):
        last = attempt == attempts - 1

        # (1) s6-rc -d change — CAUGHT (a failed s6-rc is an input, not an exit).
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["s6-rc", "-d", "change", main],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "ensure_service_down: s6-rc -d change %s failed (rc=%s) — "
                "continuing ladder", main, exc.returncode,
            )

        # (2) strict probe.
        if await _probe_service_down(scandir) == "down":
            return True

        if not last:
            # (3) supervisor fallback: capital -D latches ./down too.
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["s6-svc", "-D", scandir],
                    capture_output=True, text=True,
                )
            except OSError as exc:
                logger.warning(
                    "ensure_service_down: s6-svc -D %s failed: %s",
                    scandir, exc,
                )
            if _ENSURE_DOWN_WAIT_S:
                await asyncio.sleep(_ENSURE_DOWN_WAIT_S)
            if await _probe_service_down(scandir) == "down":
                return True
            continue

        # (4) PHYSICAL-CONTAINMENT final rung: durable latch + group SIGKILL +
        #     bounded wait, all in one supervisor command.
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["s6-svc", "-wD", "-KD", "-T", "5000", scandir],
                capture_output=True, text=True,
            )
        except OSError as exc:
            logger.warning(
                "ensure_service_down: s6-svc -wD -KD -T %s failed: %s",
                scandir, exc,
            )
        if await _probe_service_down(scandir) == "down":
            return True

        # Supervisor unresponsive AFTER the -D latch → direct group SIGKILL.
        if await _direct_killpg(scandir):
            if await _probe_service_down(scandir) == "down":
                return True

    return False


# --- A2b: verified group-wide force-turn-boundary (operator-away backstop) ---

# Poll cadence + bounds for the group-extinction verification. Module-level so
# tests drive the ladder with an injected sleep (NEVER a patch of the shared
# ``<module>.asyncio.sleep`` — memory-cage rule).
_FORCE_POLL_INTERVAL_S = 0.25
_FORCE_TERM_ATTEMPTS = 20      # after SIGTERM
_FORCE_KILL_ATTEMPTS = 8       # after the SIGKILL escalation


def _killpg(pgid: int, sig: int) -> None:
    """Thin injectable seam over ``os.killpg`` (tests monkeypatch this to record
    the exact signal sequence without touching a real process group). ``sig == 0``
    is the liveness/emptiness probe: it raises ``ProcessLookupError`` iff the
    group is extinct."""
    os.killpg(pgid, sig)


def _getpgid(pid: int) -> int:
    """Thin injectable seam over ``os.getpgid`` (tests inject a fake so the
    recorded pgid can differ from the pid without a real process). Raises
    ``ProcessLookupError`` if the pid is already gone. The run template never
    calls setsid, so the supervised leader is NOT guaranteed to be its own
    group leader — the real pgid must be READ, never assumed to equal the pid."""
    return os.getpgid(pid)


async def _poll_group_extinct(
    pgid: int, *, attempts: int, interval: float,
    sleep,
) -> bool:
    """Poll ``os.killpg(pgid, 0)`` up to ``attempts`` times (``interval`` apart)
    until it raises ``ProcessLookupError`` — i.e. the recorded process GROUP is
    empty. Returns True on extinction, False if the group is still alive after
    the last attempt. A kernel-refused probe (EPERM etc.) reads as still-alive
    (not proof of extinction)."""
    for attempt in range(attempts):
        try:
            _killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass  # e.g. EPERM — group exists, just not signalable; still alive
        # Sleep only BETWEEN probes — the timeout path (last failed probe) pays
        # no trailing sleep before returning False.
        if attempt < attempts - 1:
            await sleep(interval)
    return False


async def force_turn_boundary(
    *, engagement_id: str, sleep=asyncio.sleep,
) -> bool:
    """Force-end the engagement's CLI turn by killing its whole process GROUP,
    then VERIFY the group is extinct — the A2b operator-away hard backstop.

    s6's wanted-state is never touched, so the supervisor auto-respawns the run
    script; the fresh spawn blocks on its stdin FIFO with an empty spool — the
    architecture's natural zero-cost suspended state between turns. Ladder
    (spec §A2.6):

      1. STRICT tri-state entry probe (reuse ``_probe_service_down``):
         ``down`` → already suspended → True; ``unknown`` → WARN + return False
         truthfully ("not suspending blind" — a query failure is NOT proof);
         ``up`` → read the live pid and proceed.
      2. Record the pre-signal pgid via ``os.getpgid(pid)`` — READ, never
         assumed to equal the pid (the run template never calls setsid, so the
         leader may not be its own group leader; a hardcoded ``pgid = pid``
         would make ``os.killpg`` raise ``ProcessLookupError`` and be misread as
         "already extinct" → a FALSE verified-suspended). If ``os.getpgid``
         raises ``ProcessLookupError`` (leader vanished in the probe→getpgid
         window) the group cannot be identified → WARN + return False (never
         guess a pgid). Then ``os.killpg(recorded_pgid, SIGTERM)`` the whole
         group (``ProcessLookupError`` → group genuinely empty → True).
      3. Bounded verification of GROUP EXTINCTION (not leader turnover): poll
         ``os.killpg(recorded_pgid, 0)`` until ``ProcessLookupError``.
      4. Timeout → ``os.killpg(recorded_pgid, SIGKILL)`` (never a re-read pid — a
         respawn may already own it) → re-poll the same emptiness probe.

    Returns True IFF the old group is verifiably empty; a False is WARN-logged as
    "forced suspend NOT verified" and never falsely reported as suspended."""
    scandir = _service_scandir(engagement_id)

    status = await _probe_service_down(scandir)
    if status == "down":
        return True
    if status != "up":
        logger.warning(
            "force_turn_boundary %s: cannot verify service state — not "
            "suspending blind", engagement_id,
        )
        return False

    pid = await service_pid(engagement_id=engagement_id)
    if pid is None:
        logger.warning(
            "force_turn_boundary %s: probed up but pid unavailable — not "
            "suspending blind", engagement_id,
        )
        return False

    # Record the pgid BEFORE any signal, by READING os.getpgid — the run
    # template never calls setsid, so the leader is NOT guaranteed to be its own
    # group leader. A hardcoded ``pgid = pid`` would make killpg raise
    # ProcessLookupError on a non-leader and be misread as "already extinct" → a
    # false verified-suspended. This recorded pgid drives the SIGTERM, the
    # SIGKILL escalation and every emptiness probe (a respawn may reuse the pid
    # but never this whole group).
    try:
        pgid = _getpgid(pid)
    except ProcessLookupError:
        logger.warning(
            "force_turn_boundary %s: leader vanished before its process group "
            "could be recorded — cannot verify suspension", engagement_id,
        )
        return False
    try:
        _killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # group already extinct between probe and signal

    if await _poll_group_extinct(
        pgid, attempts=_FORCE_TERM_ATTEMPTS,
        interval=_FORCE_POLL_INTERVAL_S, sleep=sleep,
    ):
        return True

    # SIGTERM-ignoring children survive → escalate to a group SIGKILL on the
    # RECORDED pgid, then re-verify emptiness.
    try:
        _killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    if await _poll_group_extinct(
        pgid, attempts=_FORCE_KILL_ATTEMPTS,
        interval=_FORCE_POLL_INTERVAL_S, sleep=sleep,
    ):
        return True

    logger.warning(
        "force_turn_boundary %s: forced suspend NOT verified (group %s still "
        "alive after SIGKILL)", engagement_id, pgid,
    )
    return False


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
