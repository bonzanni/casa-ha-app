"""claude_code driver — s6-rc-supervised claude CLI per engagement.

See docs/superpowers/specs/2026-04-23-3.5-plan4a-claude-code-driver-design.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from drivers import s6_rc
from drivers.driver_protocol import DriverProtocol
from drivers.workspace import (
    provision_workspace, render_run_script, write_casa_meta,
)
from engagement_registry import EngagementRecord

logger = logging.getLogger(__name__)

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

    # -- DriverProtocol ---------------------------------------------------

    async def start(
        self, engagement: EngagementRecord, prompt: str, options: Any,
    ) -> None:
        """options is the ExecutorDefinition — see DriverProtocol.start docstring."""
        defn = options
        async with s6_rc._compile_lock:
            # 1. Provision workspace (CLAUDE.md, .mcp.json, plugins, FIFO, meta).
            ws = await provision_workspace(
                engagements_root=self._engagements_root,
                base_plugins_root=self._base_plugins_root,
                engagement_id=engagement.id,
                defn=defn,
                task=engagement.task,
                context=engagement.origin.get("context", ""),
                casa_framework_mcp_url=self._casa_framework_mcp_url,
            )
            write_casa_meta(
                workspace_path=ws,
                engagement_id=engagement.id,
                executor_type=defn.type,
                status="UNDERGOING",
                created_at=_iso_now(),
                finished_at=None, retention_until=None,
            )

            # 2. Write the s6 service dir.
            run_script = render_run_script(
                engagement_id=engagement.id,
                permission_mode=defn.permission_mode or "acceptEdits",
                extra_dirs=list(defn.extra_dirs),
            )
            s6_rc.write_service_dir(
                svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                engagement_id=engagement.id,
                run_script=run_script,
                depends_on=["init-setup-configs"],
            )

            # 3. Compile + update + change — lock held, inner helper.
            await s6_rc._compile_and_update_locked()
            await s6_rc.start_service(engagement_id=engagement.id)

        # 4. Kick off URL capture + respawn poller tasks (outside lock).
        self._spawn_background_tasks(engagement)

        # 5. Write initial prompt to FIFO — background, non-blocking.
        if prompt:
            asyncio.create_task(self._write_to_fifo(engagement, prompt))

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

        async with s6_rc._compile_lock:
            # Stop is tolerant of "already down" — log and continue.
            try:
                await s6_rc.stop_service(engagement_id=engagement.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stop_service(%s) failed: %s",
                               engagement.id[:8], exc)
            s6_rc.remove_service_dir(
                svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                engagement_id=engagement.id,
            )
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

    def _spawn_background_tasks(self, engagement: EngagementRecord) -> None:
        """Start URL capture + respawn poller tasks for this engagement.

        Placeholder list so cancel() doesn't KeyError. Real task spawning
        lands in Tasks C3 (URL capture) and C4 (respawn poller).
        """
        self._tasks.setdefault(engagement.id, [])


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
