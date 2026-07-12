"""claude_code driver — s6-rc-supervised claude CLI per engagement.

See docs/superpowers/specs/2026-04-23-3.5-plan4a-claude-code-driver-design.md.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from drivers import s6_rc
from drivers.driver_protocol import DriverProtocol
from drivers.workspace import (
    engagement_log_dir, provision_workspace, render_log_run_script,
    render_run_script, write_casa_meta,
)
from engagement_registry import EngagementRecord

logger = logging.getLogger(__name__)
# P31 (v0.37.10): match a UUID as a complete filename stem — the
# claude CLI names its session-storage files ``<uuid>.jsonl``. Replaces
# v0.37.9's free-text session_id regex (which tailed a log file that
# never gets created in production; see bug-review-2026-05-14-exploration6).
_UUID_REGEX = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}'
    r'-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

TopicSender = Callable[[int, str], Awaitable[None]]
SessionIdPersister = Callable[[str, str], Awaitable[None]]
"""(engagement_id, session_id) → None — registry persist hook.

Matches engagement_registry.persist_session_id's bound-method signature."""


class ClaudeCodeDriver(DriverProtocol):
    """s6-rc orchestrator. Does not manage subprocesses directly."""

    def __init__(
        self,
        *,
        engagements_root: str,
        send_to_topic: TopicSender,
        casa_framework_mcp_url: str,
        persist_session_id: SessionIdPersister | None = None,
    ) -> None:
        self._engagements_root = engagements_root
        self._send_to_topic = send_to_topic
        self._casa_framework_mcp_url = casa_framework_mcp_url
        self._persist_session_id = persist_session_id
        # Per-engagement background tasks (respawn poller, session-id
        # capture, DEBUG log relay).
        self._tasks: dict[str, list[asyncio.Task]] = {}
        self._last_turn_ts: dict[str, float] = {}

    # -- DriverProtocol ---------------------------------------------------

    async def start(
        self, engagement: EngagementRecord, prompt: str, options: Any,
    ) -> None:
        """options is the ExecutorDefinition — see DriverProtocol.start docstring.

        Bug 13 (v0.14.6): if any step from provision_workspace through
        start_service fails, roll back the partial state (workspace,
        service dir, s6-rc compile) so the engagement registry / sweeper
        don't end up with a permanent UNDERGOING ghost. The exception is
        re-raised so engage_executor's caller surfaces the failure.
        """
        import shutil
        defn = options
        # Workspace path is deterministic — compute it up front so the
        # rollback path can rmtree even if provision_workspace raises
        # before returning the assignment.
        ws_path = str(Path(self._engagements_root) / engagement.id)
        service_dir_written = False
        async with s6_rc._compile_lock:
            try:
                # M4: precompute executor_memory if the executor opts in.
                # Forward-compat — no claude_code executor opts in today, but
                # threading the slot now means a future memory-enabled
                # claude_code executor (e.g. claude_code-flavoured
                # configurator) works without further plumbing. Lazy import
                # of tools avoids a top-level cycle (drivers ← agent ← drivers);
                # _fetch_executor_archive lazily imports agent itself.
                executor_memory_block = ""
                if defn.memory.enabled:
                    from tools import _fetch_executor_archive
                    executor_memory_block = await _fetch_executor_archive(
                        task=engagement.task,
                        origin_channel=engagement.origin.get("channel", "telegram"),
                        token_budget=defn.memory.token_budget,
                    )

                # §3.3: a workspace-template/ (e.g. plugin-developer) selects
                # the template render path — independent of plugin assignment.
                exec_dir = Path(defn.prompt_template_path).parent
                template_root = exec_dir / "workspace-template"

                # 1. Provision workspace (CLAUDE.md, .mcp.json, FIFO, meta).
                ws = await provision_workspace(
                    engagements_root=self._engagements_root,
                    engagement_id=engagement.id,
                    defn=defn,
                    task=engagement.task,
                    context=engagement.origin.get("context", ""),
                    world_state_summary=engagement.origin.get("world_state_summary", ""),
                    casa_framework_mcp_url=self._casa_framework_mcp_url,
                    workspace_template_root=(
                        template_root if template_root.is_dir() else None
                    ),
                    executor_memory=executor_memory_block,
                )
                write_casa_meta(
                    workspace_path=ws,
                    engagement_id=engagement.id,
                    executor_type=defn.type,
                    status="UNDERGOING",
                    created_at=_iso_now(),
                    finished_at=None, retention_until=None,
                )

                # 2. Write the s6 service pair (sibling logger service
                #    captures the CLI's stdout — see s6_rc.write_service_dir).
                # v0.14.9: GITHUB_TOKEN is set at addon boot via
                # setup-configs.sh → /run/s6/container_environment/GITHUB_TOKEN, and
                # s6-overlay merges it into every supervised service's env. Engagement
                # subprocesses inherit it without per-engagement plumbing.
                extra_env: dict[str, str] = {}
                run_script = render_run_script(
                    engagement_id=engagement.id,
                    permission_mode=defn.permission_mode or "acceptEdits",
                    extra_dirs=list(defn.extra_dirs),
                    extra_env=extra_env or None,
                )
                log_script = render_log_run_script(engagement_id=engagement.id)
                s6_rc.write_service_dir(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=engagement.id,
                    run_script=run_script,
                    depends_on=["init-setup-configs"],
                    log_run_script=log_script,
                )
                service_dir_written = True

                # 3. Compile + update + change — lock held, inner helper.
                await s6_rc._compile_and_update_locked()
                await s6_rc.start_service(engagement_id=engagement.id)
            except Exception as start_exc:  # noqa: BLE001 — rollback is opportunistic
                logger.warning(
                    "claude_code start failed for engagement %s: %s; rolling back",
                    engagement.id[:8], start_exc,
                )
                # Best-effort rollback. Each step swallows its own errors so
                # one rollback failure doesn't mask the original cause.
                # v0.64.0: ALWAYS attempt dir removal — write_service_dir can
                # raise midway (pair half-written), and remove_service_dir is
                # idempotent. Recompile only when the dirs were fully written
                # (before that, the live db never saw them).
                try:
                    s6_rc.remove_service_dir(
                        svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                        engagement_id=engagement.id,
                    )
                except Exception as rb_exc:  # noqa: BLE001
                    logger.warning(
                        "rollback remove_service_dir failed: %s", rb_exc,
                    )
                if service_dir_written:
                    try:
                        await s6_rc._compile_and_update_locked()
                    except Exception as rb_exc:  # noqa: BLE001
                        logger.warning(
                            "rollback compile_and_update failed: %s", rb_exc,
                        )
                # Always attempt to remove the workspace tree at the
                # deterministic path — provision_workspace may have raised
                # AFTER creating partial state.
                try:
                    shutil.rmtree(ws_path, ignore_errors=True)
                except Exception as rb_exc:  # noqa: BLE001
                    logger.warning(
                        "rollback rmtree(%s) failed: %s", ws_path, rb_exc,
                    )
                raise

        # 4. Kick off the background tasks (outside lock): respawn poller,
        #    session-id capture, and (at DEBUG) the log relay.
        self._spawn_background_tasks(engagement)

        # 5. Write initial prompt to FIFO — background, non-blocking.
        if prompt:
            prompt_task = asyncio.create_task(
                self._write_to_fifo(engagement, prompt),
                name=f"initial_prompt:{engagement.id[:8]}",
            )
            self._tasks.setdefault(engagement.id, []).append(prompt_task)

        logger.info("claude_code engagement %s started", engagement.id[:8])

    async def send_user_turn(
        self, engagement: EngagementRecord, text: str,
    ) -> None:
        await self._write_to_fifo(engagement, text)

    async def cancel(self, engagement: EngagementRecord) -> None:
        """Teardown for a terminal transition (cancelled or completed)."""
        # Cancel background tasks
        for t in self._tasks.pop(engagement.id, []):
            t.cancel()
        self._last_turn_ts.pop(engagement.id, None)

        async with s6_rc._compile_lock:
            # Stop is tolerant of "already down" — log and continue.
            try:
                await s6_rc.stop_service(engagement_id=engagement.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stop_service(%s) failed: %s",
                               engagement.id[:8], exc)
            # v0.64.0: also stop the sibling logger service explicitly so the
            # recompile below never has to down a still-live service. No-op
            # for legacy engagements without one.
            try:
                await s6_rc.stop_log_service(engagement_id=engagement.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stop_log_service(%s) failed: %s",
                               engagement.id[:8], exc)
            try:
                s6_rc.remove_service_dir(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=engagement.id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("remove_service_dir(%s) failed: %s",
                               engagement.id[:8], exc)
            try:
                await s6_rc._compile_and_update_locked()
            except Exception as exc:  # noqa: BLE001
                logger.warning("compile_and_update after remove failed: %s", exc)

    async def resume(self, engagement: EngagementRecord, session_id: str) -> None:
        """Effectively a no-op under s6 — the run script reads .session_id on
        its next spawn. Included for DriverProtocol completeness."""
        return

    def is_alive(self, engagement: EngagementRecord) -> bool:
        """Synchronous probe — schedules an async s6-svstat call and waits.

        Called from sweep code that is already async; use is_alive_async
        if you need awaitable form. This sync wrapper exists only to
        match DriverProtocol.is_alive signature.
        """
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Can't block the running loop; return True optimistically.
            # Callers in async context should use is_alive_async().
            return engagement.id in self._tasks
        return loop.run_until_complete(self.is_alive_async(engagement))

    async def is_alive_async(self, engagement: EngagementRecord) -> bool:
        pid = await s6_rc.service_pid(engagement_id=engagement.id)
        return pid is not None

    # -- internal ---------------------------------------------------------

    async def _write_to_fifo(
        self, engagement: EngagementRecord, text: str,
        *, timeout_s: float = 20.0, poll_s: float = 0.25,
    ) -> None:
        # M13: a blocking open(fifo, "a") parks a pooled executor thread
        # FOREVER when no reader exists (crash-looping/downed s6 service).
        # asyncio.to_thread threads are uncancellable, so a handful of stuck
        # writes starve all subprocess orchestration app-wide. Open + write
        # non-blocking with a bounded deadline instead — no thread at all.
        fifo = (Path(self._engagements_root) / engagement.id / "stdin.fifo")
        if not fifo.exists():
            logger.warning("FIFO missing for engagement %s", engagement.id[:8])
            return
        data = (text + "\n").encode("utf-8")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        fd: int | None = None
        try:
            # O_NONBLOCK open raises ENXIO while no reader exists — retry until
            # a reader appears or the deadline passes (covers the ~1s s6
            # respawn pause without ever parking a thread).
            while fd is None:
                try:
                    fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
                except OSError as exc:
                    if exc.errno != errno.ENXIO:
                        logger.warning(
                            "FIFO open failed for engagement %s: %s",
                            engagement.id[:8], exc,
                        )
                        return
                    if loop.time() >= deadline:
                        logger.warning(
                            "engagement %s: no FIFO reader after %.0fs — "
                            "dropping turn", engagement.id[:8], timeout_s,
                        )
                        await self._send_to_topic(
                            engagement.topic_id,
                            "The engagement isn't accepting input right now — "
                            "your message was not delivered. Try again, or "
                            "/cancel if it stays unresponsive.",
                        )
                        return
                    await asyncio.sleep(poll_s)
            # Reader exists; write non-blocking under the same deadline. Turns
            # are far below the 64KB pipe buffer, so the first write virtually
            # always completes fully.
            view = memoryview(data)
            while view:
                try:
                    n = os.write(fd, view)
                    view = view[n:]
                except BlockingIOError:
                    if loop.time() >= deadline:
                        logger.warning(
                            "engagement %s: FIFO write stalled — dropping "
                            "remainder of turn", engagement.id[:8],
                        )
                        return
                    await asyncio.sleep(poll_s)
                except BrokenPipeError:
                    logger.warning(
                        "engagement %s: FIFO reader vanished mid-write",
                        engagement.id[:8],
                    )
                    return
        finally:
            if fd is not None:
                os.close(fd)
        self._last_turn_ts[engagement.id] = time.time()

    def _spawn_background_tasks(self, engagement: EngagementRecord) -> None:
        tasks = [
            asyncio.create_task(self._poll_respawns(engagement)),
            # P31 (v0.37.10): capture the SDK session_id by watching the
            # claude CLI's own session-storage directory. Persists the
            # UUID to ``<workspace>/.session_id`` so the run script's
            # ``--resume $(cat .session_id)`` plumbing picks up after a
            # Casa restart.
            asyncio.create_task(self._capture_session_id(engagement)),
        ]
        # Phase 4b G5: relay every s6-log line into Casa's logger at DEBUG
        # so operators have one greppable namespace for both drivers' CLI
        # subprocess output. Spawned only when DEBUG-enabled: the tailer
        # re-opens and reads the (v0.64.0: now real) file at 10 Hz, and at
        # INFO every line would be discarded. A LOG_LEVEL flip requires an
        # add-on restart, which respawns these tasks anyway.
        if logging.getLogger("subprocess_cli").isEnabledFor(logging.DEBUG):
            log_path = os.path.join(
                engagement_log_dir(engagement.id), "current")
            tasks.append(asyncio.create_task(
                self._relay_log_lines(engagement, log_path=log_path)))
        self._tasks[engagement.id] = tasks

    async def _relay_log_lines(
        self, engagement: EngagementRecord, *, log_path: str,
    ) -> None:
        """Tail the per-engagement s6-log file and emit each line at DEBUG.

        Phase 4b G5 — companion to Bug 4's stderr callback. Stderr from the
        in_casa-driver path lands on the ``subprocess_cli`` logger via the
        SDK callback (sdk_logging.make_stderr_logger); claude_code's CLI
        subprocess merges its own stdout+stderr into s6-log
        (engagement_run_template.sh ``exec 2>&1``). This task reuses
        ``_tail_file`` so inode rotation is handled, and emits every line at
        DEBUG so prod operators see nothing and debugging operators see
        everything (single LOG_LEVEL=DEBUG flip).

        v0.64.0 removed the sibling ``_capture_url`` task: headless claude
        auto-degrades to one-shot --print mode on non-TTY stdout and never
        prints a remote-control URL line, so there is nothing to capture
        (live-verified; see the 2026-07-10 remote-control-honesty design).
        """
        short = engagement.id[:8]
        relay_logger = logging.getLogger("subprocess_cli")
        async for line in _tail_file(log_path, from_end=True):
            relay_logger.debug(
                "stdout %s", line.rstrip("\n"),
                extra={"engagement_id": short},
            )

    async def _capture_session_id(
        self, engagement: EngagementRecord, *,
        poll_interval_s: float = 0.5,
    ) -> None:
        """P31 (v0.37.10): watch the claude CLI's own session-storage
        directory for the first ``<uuid>.jsonl`` file. The filename
        (minus extension) IS the SDK session UUID. Persist to
        ``<workspace>/.session_id`` so a boot-replay's
        ``--resume $(cat .session_id)`` flag carries the conversation
        forward.

        Replaces v0.37.9's s6-log tailing approach, which was
        non-functional at the time: until v0.64.0 the s6-rc log pipeline
        was never compiled (nested log/ subdir — see
        ``s6_rc.write_service_dir``), so the log file did not exist.
        Watching the CLI's own session storage is retained even now that
        the log pipeline works: it observes the authoritative artifact
        directly. Bug-review:
        ``docs/bug-review-2026-05-14-exploration6.md::O-5``.

        Claude CLI session storage layout (HOME=<ws>/.home, CWD=<ws>):

            <ws>/.home/.claude/projects/-data-engagements-<id>/<uuid>.jsonl

        The directory-name encoding replaces ``/`` with ``-`` in the
        workspace path (claude CLI native behavior).

        One-shot: returns after the first UUID-named .jsonl is found.
        Re-spawns on s6 restart see the persisted file and resume
        cleanly — see ``engagement_run_template.sh``.

        Atomic write: temp-file + ``os.replace`` so a Casa crash
        mid-write cannot leave a half-truncated ``.session_id``.
        """
        short = engagement.id[:8]
        ws = Path(self._engagements_root) / engagement.id
        target = ws / ".session_id"
        tmp = ws / ".session_id.tmp"
        projects_dir = (
            ws / ".home" / ".claude" / "projects"
            / f"-data-engagements-{engagement.id}"
        )
        while True:
            sid = self._scan_projects_dir_for_sid(projects_dir)
            if sid is not None:
                try:
                    tmp.write_text(sid + "\n", encoding="utf-8")
                    os.replace(tmp, target)
                except OSError as exc:
                    logger.warning(
                        "engagement %s session_id persist failed: %s",
                        short, exc,
                    )
                    return
                logger.info(
                    "engagement %s captured sdk session_id %s",
                    short, sid[:8],
                )
                if self._persist_session_id is not None:
                    try:
                        await self._persist_session_id(engagement.id, sid)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "engagement %s persist_session_id callback "
                            "failed: %s", short, exc,
                        )
                return
            await asyncio.sleep(poll_interval_s)

    @staticmethod
    def _scan_projects_dir_for_sid(projects_dir: Path) -> str | None:
        """Return the oldest UUID-named .jsonl in projects_dir, or None.

        Sort by mtime ascending so the first session file (the one
        spawned by the initial CLI start) wins over any later ones the
        CLI might write on a resume retry.
        """
        try:
            if not projects_dir.is_dir():
                return None
            candidates: list[tuple[float, str]] = []
            for p in projects_dir.iterdir():
                if p.suffix != ".jsonl":
                    continue
                stem = p.stem
                if _UUID_REGEX.match(stem) is None:
                    continue
                try:
                    candidates.append((p.stat().st_mtime, stem))
                except OSError:
                    continue
            if not candidates:
                return None
            candidates.sort()
            return candidates[0][1]
        except OSError:
            return None

    async def _poll_respawns(
        self, engagement: EngagementRecord, *, interval_s: float = 5.0,
    ) -> None:
        """Emit subprocess_respawn bus events when s6-svstat shows a new PID."""
        last_pid: int | None = None
        while True:
            await asyncio.sleep(interval_s)
            pid = await s6_rc.service_pid(engagement_id=engagement.id)
            if pid is None:
                continue
            if last_pid is not None and pid != last_pid:
                await self._publish_bus_event({
                    "event": "subprocess_respawn",
                    "engagement_id": engagement.id,
                    "previous_pid": last_pid,
                    "new_pid": pid,
                    "ts": time.time(),
                })
                await self._maybe_warn_of_lost_turn(engagement)
            last_pid = pid

    async def _publish_bus_event(self, event: dict) -> None:
        """Overridable (tests inject). Default no-op at driver layer —
        casa_core wires a real bus sink in at construction time (see Phase E)."""
        logger.debug("bus event (no sink wired): %s", event)

    async def _maybe_warn_of_lost_turn(
        self, engagement: EngagementRecord,
    ) -> None:
        """If the last send_user_turn was within 5 seconds of now, post a
        topic warning that the turn may have been lost during respawn."""
        last_ts = self._last_turn_ts.get(engagement.id)
        if last_ts is None:
            return
        if time.time() - last_ts < 5.0:
            await self._send_to_topic(
                engagement.topic_id,
                "Your last message may not have reached the engagement — "
                "please retype it.",
            )


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _tail_file(log_path: str, *, from_end: bool = False):
    """Yield new lines from a file as they appear. Terminates on task cancel.

    Bug 11 (v0.14.6): tracks the file's inode so rotation is handled.
    s6-log rotates ``current`` at 1 MB by renaming it to ``@<timestamp>.s``
    and creating a fresh ``current``. Pre-fix the loop kept seeking to
    the OLD pos in the new (smaller) file, so all lines below the prior
    cutoff were silently dropped. Now: when ``st_ino`` changes, reset
    ``pos`` to 0 so the new file is read from its start. We also reset
    if the file shrinks below ``pos`` (truncate-in-place pattern).

    v0.64.0 (file is now real in production):
      - ``from_end=True`` starts at the file's current end when it already
        exists at first sight — boot replay re-attaches without re-yielding
        up to 1 MB of history. A file that appears later (fresh engagement)
        is still read from its start.
      - A transient OSError mid-cycle (rotation renames ``current`` between
        ``exists()`` and ``open()``) retries next tick instead of killing
        the (unobserved) consumer task.
    """
    path = Path(log_path)
    pos = 0
    last_inode: int | None = None
    first_sight = True
    while True:
        try:
            exists = path.exists()
            if first_sight:
                first_sight = False
                if exists and from_end:
                    try:
                        pos = path.stat().st_size
                    except OSError:
                        pos = 0
            if exists:
                try:
                    current_inode = path.stat().st_ino
                except OSError:
                    current_inode = None
                if last_inode is not None and current_inode != last_inode:
                    pos = 0
                last_inode = current_inode

                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(0, 2)            # SEEK_END
                    end = fh.tell()
                    if pos > end:
                        pos = 0
                    fh.seek(pos)
                    while True:
                        line = fh.readline()
                        if not line:
                            pos = fh.tell()
                            break
                        yield line
        except OSError:
            pass                             # transient — retry next tick
        await asyncio.sleep(0.1)
