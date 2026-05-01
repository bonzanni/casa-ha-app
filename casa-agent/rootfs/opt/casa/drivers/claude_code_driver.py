"""claude_code driver — s6-rc-supervised claude CLI per engagement.

See docs/superpowers/specs/2026-04-23-3.5-plan4a-claude-code-driver-design.md.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from drivers import s6_rc
from drivers.driver_protocol import DriverProtocol
from drivers.workspace import (
    provision_workspace, render_log_run_script, render_run_script, write_casa_meta,
)
from engagement_registry import EngagementRecord

logger = logging.getLogger(__name__)
_URL_REGEX = re.compile(r"Remote Control URL:\s+(https?://\S+)")

TopicSender = Callable[[int, str], Awaitable[None]]


class ClaudeCodeDriver(DriverProtocol):
    """s6-rc orchestrator. Does not manage subprocesses directly."""

    def __init__(
        self,
        *,
        engagements_root: str,
        base_plugins_root: str,
        send_to_topic: TopicSender,
        casa_framework_mcp_url: str,
    ) -> None:
        self._engagements_root = engagements_root
        self._base_plugins_root = base_plugins_root
        self._send_to_topic = send_to_topic
        self._casa_framework_mcp_url = casa_framework_mcp_url
        # Per-engagement background tasks (URL capture, respawn poller).
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
                # configurator) works without further plumbing. Lazy imports
                # avoid a top-level cycle (drivers ← agent ← drivers).
                executor_memory_block = ""
                if defn.memory.enabled:
                    import agent as agent_mod
                    from tools import _fetch_executor_archive
                    executor_memory_block = await _fetch_executor_archive(
                        memory_provider=getattr(
                            agent_mod, "active_memory_provider", None,
                        ),
                        channel=engagement.origin.get("channel", "telegram"),
                        chat_id=str(engagement.origin.get("chat_id", "")),
                        executor_type=defn.type,
                        token_budget=defn.memory.token_budget,
                    )

                # L-1 (v0.34.2): pass workspace_template_root + plugins_yaml so
                # claude_code executors with a workspace-template/ (e.g.
                # plugin-developer) flow through the template path.
                exec_dir = Path(defn.prompt_template_path).parent
                template_root = exec_dir / "workspace-template"
                plugins_yaml_path = exec_dir / "plugins.yaml"

                # 1. Provision workspace (CLAUDE.md, .mcp.json, plugins, FIFO, meta).
                ws = await provision_workspace(
                    engagements_root=self._engagements_root,
                    base_plugins_root=self._base_plugins_root,
                    engagement_id=engagement.id,
                    defn=defn,
                    task=engagement.task,
                    context=engagement.origin.get("context", ""),
                    casa_framework_mcp_url=self._casa_framework_mcp_url,
                    workspace_template_root=(
                        template_root if template_root.is_dir() else None
                    ),
                    plugins_yaml=(
                        plugins_yaml_path if plugins_yaml_path.is_file() else None
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

                # 2. Write the s6 service dir (with log sub-service for stdout capture).
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
                if service_dir_written:
                    try:
                        s6_rc.remove_service_dir(
                            svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                            engagement_id=engagement.id,
                        )
                    except Exception as rb_exc:  # noqa: BLE001
                        logger.warning(
                            "rollback remove_service_dir failed: %s", rb_exc,
                        )
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

        # 4. Kick off URL capture + respawn poller tasks (outside lock).
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
    ) -> None:
        fifo = (Path(self._engagements_root) / engagement.id / "stdin.fifo")
        if not fifo.exists():
            logger.warning("FIFO missing for engagement %s", engagement.id[:8])
            return
        # Open in a thread — Python's open() on a FIFO blocks until reader is present.
        def _write():
            with open(fifo, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        await asyncio.to_thread(_write)
        self._last_turn_ts[engagement.id] = time.time()

    def _spawn_background_tasks(self, engagement: EngagementRecord) -> None:
        log_path = f"/var/log/casa-engagement-{engagement.id}/current"
        self._tasks[engagement.id] = [
            asyncio.create_task(self._capture_url(engagement, log_path=log_path)),
            asyncio.create_task(self._poll_respawns(engagement)),
            # Phase 4b G5: relay every s6-log line into Casa's logger at
            # DEBUG so operators have one greppable namespace for both
            # drivers' CLI subprocess output. Independent tailer — the
            # _capture_url task drops non-URL lines.
            asyncio.create_task(self._relay_log_lines(engagement, log_path=log_path)),
        ]

    async def _capture_url(
        self, engagement: EngagementRecord, *,
        log_path: str, initial_window_s: float = 60.0,
    ) -> None:
        """Persistent tail — posts topic notices on URL change only."""
        last_seen: str | None = None
        initial_posted_warning = False
        deadline = asyncio.get_event_loop().time() + initial_window_s

        async for line in _tail_file(log_path):
            m = _URL_REGEX.search(line)
            if not m:
                now = asyncio.get_event_loop().time()
                if (not initial_posted_warning and last_seen is None
                        and now > deadline):
                    await self._send_to_topic(
                        engagement.topic_id,
                        "Remote control URL not yet available — Telegram-only "
                        "for now. Will post here if it becomes available later.",
                    )
                    initial_posted_warning = True
                continue

            url = m.group(1)
            if url == last_seen:
                continue
            last_seen = url
            await self._send_to_topic(
                engagement.topic_id,
                f"Remote control: {url} — open in iOS app or browser "
                f"to drive from anywhere.",
            )

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

        Distinct from ``_capture_url`` — that task seeks the URL regex and
        drops every other line. This task is the catch-all diagnostic
        relay; both tasks share the same log_path but tail it
        independently.
        """
        short = engagement.id[:8]
        relay_logger = logging.getLogger("subprocess_cli")
        async for line in _tail_file(log_path):
            relay_logger.debug(
                "stdout %s", line.rstrip("\n"),
                extra={"engagement_id": short},
            )

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


async def _tail_file(log_path: str):
    """Yield new lines from a file as they appear. Terminates on task cancel.

    Bug 11 (v0.14.6): tracks the file's inode so rotation is handled.
    s6-log rotates ``current`` at 1 MB by renaming it to ``@<timestamp>.s``
    and creating a fresh ``current``. Pre-fix the loop kept seeking to
    the OLD pos in the new (smaller) file, so all lines below the prior
    cutoff were silently dropped. Now: when ``st_ino`` changes, reset
    ``pos`` to 0 so the new file is read from its start. We also reset
    if the file shrinks below ``pos`` (truncate-in-place pattern).
    """
    path = Path(log_path)
    pos = 0
    last_inode: int | None = None
    while True:
        try:
            if path.exists():
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
        except (OSError, asyncio.CancelledError):
            raise
        await asyncio.sleep(0.1)
