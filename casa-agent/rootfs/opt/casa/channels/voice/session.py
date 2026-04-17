"""Voice session bookkeeping — pool + idle sweep + prewarm dedup.

Pool is process-local. Cleared on restart. Eviction cancels any live
prewarm task but does NOT evict the remote Honcho session (those are
persistent).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from session_registry import build_session_key

logger = logging.getLogger(__name__)


@dataclass
class VoiceSession:
    scope_id: str
    session_key: str
    last_activity: float
    gate: asyncio.Semaphore
    prewarm_task: asyncio.Task | None = None
    in_flight: str | None = None


class VoiceSessionPool:
    def __init__(self, idle_timeout: int, gate_slots: int = 10) -> None:
        self._idle_timeout = idle_timeout
        self._gate_slots = gate_slots
        self._sessions: dict[str, VoiceSession] = {}

    # --- lifecycle ----------------------------------------------------

    def ensure(self, scope_id: str) -> VoiceSession:
        sess = self._sessions.get(scope_id)
        if sess is not None:
            return sess
        sess = VoiceSession(
            scope_id=scope_id,
            session_key=build_session_key("voice", scope_id),
            last_activity=time.monotonic(),
            gate=asyncio.Semaphore(self._gate_slots),
        )
        self._sessions[scope_id] = sess
        return sess

    def get(self, scope_id: str) -> VoiceSession | None:
        return self._sessions.get(scope_id)

    def touch(self, scope_id: str) -> None:
        sess = self._sessions.get(scope_id)
        if sess is not None:
            sess.last_activity = time.monotonic()

    def sweep(self) -> list[str]:
        """Evict sessions idle longer than idle_timeout. Returns evicted scope_ids."""
        now = time.monotonic()
        evicted: list[str] = []
        for scope_id, sess in list(self._sessions.items()):
            if now - sess.last_activity > self._idle_timeout:
                if sess.prewarm_task is not None and not sess.prewarm_task.done():
                    sess.prewarm_task.cancel()
                    sess.prewarm_task = None
                self._sessions.pop(scope_id, None)
                evicted.append(scope_id)
        if evicted:
            logger.info("Voice pool evicted %d idle session(s)", len(evicted))
        return evicted

    # --- prewarm ------------------------------------------------------

    def schedule_prewarm(
        self,
        scope_id: str,
        coro_factory: Callable[[], Awaitable[None]],
    ) -> asyncio.Task | None:
        """Kick a prewarm coroutine if none is live for this scope.

        Returns the task (or ``None`` if dedup'd). ``coro_factory`` is
        called *here* so repeated calls don't construct stray coroutines.
        """
        sess = self.ensure(scope_id)
        if sess.prewarm_task is not None and not sess.prewarm_task.done():
            return None
        task = asyncio.create_task(coro_factory())
        sess.prewarm_task = task
        return task

    # --- background sweeper -------------------------------------------

    async def run_sweeper(self, interval: float = 30.0) -> None:
        """Long-lived task; cancel on shutdown."""
        try:
            while True:
                await asyncio.sleep(interval)
                self.sweep()
        except asyncio.CancelledError:
            pass
