"""Periodic session-registry TTL sweep (spec 5.2 §6).

Stops `SessionRegistry.sessions.json` from growing unbounded. Every
`sweep_interval_hours` (default 6) a background task wakes up, iterates
the registry under its 5.1 lock, drops entries whose `last_active` is
older than the configured TTL, persists the reduced registry, and
best-effort attempts to prune the corresponding SDK session on
Anthropic's side if the SDK exposes a `delete_session` call.

Pure policy module — no transport imports. Reaches into internal
`SessionRegistry` attributes (`_lock`, `_data`, `_save_locked`) by
design: 5.1 left those seams for internal consumers exactly so we
don't have to contort the public API for policy layers that sit above
the storage layer.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from session_registry import SessionRegistry

logger = logging.getLogger(__name__)

# Sweep cadence — hard-coded per spec R5 (periodic sweep is cheap: one
# pass over < 100 entries, once every 6 h). Not on the env-var surface
# (§9.3 lists only SESSION_TTL_DAYS and WEBHOOK_SESSION_TTL_DAYS).
_SWEEP_INTERVAL_HOURS = 6

# Defaults, exported for casa_core fallback.
_DEFAULT_SESSION_TTL_DAYS = 30
_DEFAULT_WEBHOOK_SESSION_TTL_DAYS = 1


def _is_uuid_scope(scope_id: str) -> bool:
    """True when `scope_id` parses as a UUID (any version).

    Used to distinguish a webhook one-shot (random chat_id fabricated
    by `build_invoke_message`) from a deliberately-pinned webhook
    session (e.g. `webhook:ha-automation-daily`). Only the former
    qualifies for the short `webhook_session_ttl_days`.
    """
    try:
        uuid.UUID(scope_id)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _parse_last_active(value: object) -> datetime | None:
    """Parse an ISO `last_active` string; return None on garbage / missing."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _prune_sdk_session(sdk_session_id: str) -> None:
    """Best-effort SDK-side session cleanup.

    Looks up `claude_agent_sdk.delete_session` via `getattr`. If the
    SDK does not expose one (today: always), this is a no-op. If it
    does, the call is awaited (or invoked sync-ly if it is not a
    coroutine) and any exception is swallowed at DEBUG level — the
    eviction in the local registry is the source of truth.
    """
    try:
        import claude_agent_sdk as _sdk
    except ImportError:
        return
    fn = getattr(_sdk, "delete_session", None)
    if fn is None or not callable(fn):
        return
    try:
        result = fn(sdk_session_id)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug(
            "SDK delete_session(%s) failed: %s", sdk_session_id, exc,
        )


class SessionSweeper:
    """Periodic TTL-based eviction of stale `SessionRegistry` entries.

    Use::

        sweeper = SessionSweeper(
            registry=session_registry,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
        )
        sweeper.start()
        ...
        await sweeper.stop()
    """

    def __init__(
        self,
        *,
        registry: SessionRegistry,
        session_ttl_days: int = _DEFAULT_SESSION_TTL_DAYS,
        webhook_session_ttl_days: int = _DEFAULT_WEBHOOK_SESSION_TTL_DAYS,
        sweep_interval_hours: float = _SWEEP_INTERVAL_HOURS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._session_ttl = timedelta(days=session_ttl_days)
        self._webhook_ttl = timedelta(days=webhook_session_ttl_days)
        self._interval_s = sweep_interval_hours * 3600.0
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the sweep task. Idempotent — second call is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="session-sweeper")
        logger.info(
            "Session sweeper started (session_ttl=%dd, webhook_ttl=%dd, "
            "interval=%.1fh)",
            self._session_ttl.days, self._webhook_ttl.days,
            self._interval_s / 3600.0,
        )

    async def stop(self) -> None:
        """Cancel the sweep task and wait for it to exit."""
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    # ------------------------------------------------------------------
    # Internals — exposed for test invocation
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await asyncio.sleep(self._interval_s)
                except asyncio.CancelledError:
                    raise
                try:
                    await self._sweep_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # A sweep failure must not crash the loop; next tick
                    # retries. Log loudly so operators notice.
                    logger.error(
                        "Session sweep failed; will retry next tick: %s", exc,
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            return

    async def _sweep_once(self) -> None:
        """Run one full sweep: classify + evict under lock, then prune SDK."""
        now = self._now()
        to_evict: list[tuple[str, str]] = []  # (key, sdk_session_id)

        async with self._registry._lock:
            for key, entry in self._registry._data.items():
                channel, _, scope_id = key.partition(":")
                last_active = _parse_last_active(entry.get("last_active"))

                if last_active is None:
                    to_evict.append((key, str(entry.get("sdk_session_id") or "")))
                    continue

                if channel == "webhook" and _is_uuid_scope(scope_id):
                    ttl = self._webhook_ttl
                else:
                    ttl = self._session_ttl

                if now - last_active > ttl:
                    to_evict.append(
                        (key, str(entry.get("sdk_session_id") or "")),
                    )

            if not to_evict:
                return  # no save, no SDK-prune work

            for key, _sdk_sid in to_evict:
                self._registry._data.pop(key, None)
            await self._registry._save_locked()

        logger.info("Session sweep evicted %d entr(y|ies)", len(to_evict))

        # SDK-side prune happens OUTSIDE the lock — best-effort, never
        # blocks the registry.
        for _key, sdk_sid in to_evict:
            if sdk_sid:
                await _prune_sdk_session(sdk_sid)
