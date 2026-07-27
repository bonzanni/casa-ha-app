"""Telegram reconnect supervisor (spec 5.2 §4).

Owns an async task that reconnects the Telegram Application with
1s → 60s jittered exponential backoff. Runs forever until cancelled.
Logs ONE error on first failure per outage and ONE info on recovery.

Pure module — no `telegram` imports. Takes a zero-arg async rebuild
callback so the caller owns the transport-specific work.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from retry import compute_backoff_ms


class ReconnectSupervisor:
    """Single-instance async supervisor for Telegram transport reconnect.

    Use:
        sup = ReconnectSupervisor(rebuild_fn=channel._rebuild, logger=log)
        sup.start()
        # on any detected transport failure:
        sup.trigger("probe_failed: ...")
        # on shutdown:
        await sup.stop()
    """

    def __init__(
        self,
        *,
        rebuild_fn: Callable[[], Awaitable[None]],
        logger: logging.Logger,
        initial_ms: int = 1000,
        cap_ms: int = 60_000,
    ) -> None:
        self._rebuild_fn = rebuild_fn
        self._logger = logger
        self._initial_ms = initial_ms
        self._cap_ms = cap_ms
        self._trigger_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        # Log-once state for the current outage.
        self._outage_active = False
        self._last_reason = ""

    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the supervisor task. Idempotent — second call is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="telegram-supervisor")

    async def stop(self) -> None:
        """Cancel the supervisor task and wait for it to exit."""
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    def trigger(self, reason: str) -> None:
        """Request a reconnect cycle. Coalesced while one is already running."""
        self._last_reason = reason
        self._trigger_event.set()

    # ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            while True:
                await self._trigger_event.wait()
                self._trigger_event.clear()
                await self._reconnect_loop()
        except asyncio.CancelledError:
            return

    async def _reconnect_loop(self) -> None:
        """Retry rebuild_fn with backoff until it succeeds.

        First failure in an outage → logger.error once. Recovery after a
        logged failure → logger.info once. If the very first attempt
        succeeds, neither line fires.
        """
        attempt = 0
        while True:
            try:
                await self._rebuild_fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if not self._outage_active:
                    self._outage_active = True
                    self._logger.error(
                        "Telegram transport failed (%s): %s — entering reconnect loop",
                        self._last_reason, exc,
                    )
                delay_ms = compute_backoff_ms(
                    attempt, initial_ms=self._initial_ms, cap_ms=self._cap_ms,
                )
                self._logger.debug(
                    "Telegram reconnect attempt %d failed (%s); backoff %dms",
                    attempt + 1, exc, delay_ms,
                )
                try:
                    await asyncio.sleep(delay_ms / 1000.0)
                except asyncio.CancelledError:
                    raise
                attempt += 1
                continue
            # Success.
            if self._outage_active:
                self._logger.info(
                    "Telegram transport recovered after %d attempt(s)", attempt + 1,
                )
                self._outage_active = False
            return
