"""Persistent session registry backed by a JSON file."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any


def build_session_key(channel: str, scope_id: str | None) -> str:
    """Build a canonical session key of the form ``{channel}:{scope_id}``.

    The format is project-wide (Telegram, voice, webhooks, scheduled).
    ``scope_id`` may contain colons; they are preserved verbatim.
    Empty or ``None`` scope IDs become ``"default"``.
    """
    if not channel:
        raise ValueError("channel is required")
    sid = scope_id if scope_id else "default"
    return f"{channel}:{sid}"


class SessionRegistry:
    """Maps channel keys to session metadata and persists to disk.

    All disk I/O is offloaded to a thread via :func:`asyncio.to_thread`
    to avoid blocking the event loop. A single :class:`asyncio.Lock`
    serialises every mutate+save so concurrent bus tasks cannot clobber
    each other's writes (spec 5.1 §3).
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)

    async def register(
        self,
        channel_key: str,
        agent: str,
        sdk_session_id: str,
    ) -> None:
        """Register (or overwrite) a session entry and persist.

        The Honcho session ID is *not* tracked here in the 2.2a
        topology: it is derived at call time as
        ``f"{channel_key}:{agent}"``. If a legacy entry on disk has a
        ``memory_session_id`` field, it is dropped on first write.
        """
        async with self._lock:
            self._data[channel_key] = {
                "agent": agent,
                "sdk_session_id": sdk_session_id,
                "last_active": datetime.now(timezone.utc).isoformat(),
            }
            await self._save_locked()

    def get(self, channel_key: str) -> dict[str, Any] | None:
        """Return the entry for *channel_key*, or ``None``."""
        return self._data.get(channel_key)

    async def touch(self, channel_key: str) -> None:
        """Update ``last_active`` for an existing entry and persist.

        Also drops any obsolete ``memory_session_id`` field from a
        pre-2.2a entry — lazy migration, safe to re-run.
        """
        async with self._lock:
            entry = self._data.get(channel_key)
            if entry is not None:
                entry.pop("memory_session_id", None)
                entry["last_active"] = datetime.now(timezone.utc).isoformat()
                await self._save_locked()

    async def remove(self, channel_key: str) -> None:
        """Remove an entry and persist."""
        async with self._lock:
            self._data.pop(channel_key, None)
            await self._save_locked()

    async def save(self) -> None:
        """Public save: acquires the lock.

        For out-of-band callers (tests, future background snapshotters).
        Internal mutators call :meth:`_save_locked` while already
        holding the lock.
        """
        async with self._lock:
            await self._save_locked()

    async def _save_locked(self) -> None:
        """Persist current data. Caller MUST hold ``self._lock``."""
        data = dict(self._data)  # snapshot for thread safety
        await asyncio.to_thread(self._write, data)

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        """Synchronous write helper, runs in thread pool."""
        with open(self._path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    def all_entries(self) -> dict[str, dict[str, Any]]:
        """Return a shallow copy of all entries."""
        return dict(self._data)
