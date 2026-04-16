"""Persistent session registry backed by a JSON file."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any


class SessionRegistry:
    """Maps channel keys to session metadata and persists to disk.

    All disk I/O is offloaded to a thread via :func:`asyncio.to_thread`
    to avoid blocking the event loop.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict[str, dict[str, Any]] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)

    async def register(
        self,
        channel_key: str,
        agent: str,
        sdk_session_id: str,
        memory_session_id: str,
    ) -> None:
        """Register (or overwrite) a session entry and persist."""
        self._data[channel_key] = {
            "agent": agent,
            "sdk_session_id": sdk_session_id,
            "memory_session_id": memory_session_id,
            "last_active": datetime.now(timezone.utc).isoformat(),
        }
        await self.save()

    def get(self, channel_key: str) -> dict[str, Any] | None:
        """Return the entry for *channel_key*, or ``None``."""
        return self._data.get(channel_key)

    async def touch(self, channel_key: str) -> None:
        """Update ``last_active`` for an existing entry and persist."""
        entry = self._data.get(channel_key)
        if entry is not None:
            entry["last_active"] = datetime.now(timezone.utc).isoformat()
            await self.save()

    async def remove(self, channel_key: str) -> None:
        """Remove an entry and persist."""
        self._data.pop(channel_key, None)
        await self.save()

    async def save(self) -> None:
        """Write the current data to the JSON file (off-thread)."""
        data = dict(self._data)  # snapshot for thread safety
        await asyncio.to_thread(self._write, data)

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        """Synchronous write helper, runs in thread pool."""
        with open(self._path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    def all_entries(self) -> dict[str, dict[str, Any]]:
        """Return a shallow copy of all entries."""
        return dict(self._data)
