"""Tests for session_registry.py."""

import asyncio
import time

import pytest

from session_registry import SessionRegistry

pytestmark = pytest.mark.asyncio


class TestSessionRegistry:
    async def test_register_and_get(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("tg:123", "ellen", "sdk-1", "mem-1")

        entry = reg.get("tg:123")
        assert entry is not None
        assert entry["agent"] == "ellen"
        assert entry["sdk_session_id"] == "sdk-1"
        assert entry["memory_session_id"] == "mem-1"
        assert "last_active" in entry

    async def test_get_missing_returns_none(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        assert reg.get("nonexistent") is None

    async def test_touch_updates_last_active(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("tg:123", "ellen", "sdk-1", "mem-1")

        ts1 = reg.get("tg:123")["last_active"]
        await asyncio.sleep(0.05)
        await reg.touch("tg:123")
        ts2 = reg.get("tg:123")["last_active"]

        assert ts2 >= ts1

    async def test_persistence(self, tmp_path):
        """Data survives across two instances."""
        path = str(tmp_path / "sessions.json")

        r1 = SessionRegistry(path)
        await r1.register("tg:42", "tina", "sdk-2", "mem-2")

        r2 = SessionRegistry(path)
        entry = r2.get("tg:42")
        assert entry is not None
        assert entry["agent"] == "tina"

    async def test_remove(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("tg:99", "ellen", "sdk-3", "mem-3")
        assert reg.get("tg:99") is not None

        await reg.remove("tg:99")
        assert reg.get("tg:99") is None

    async def test_all_entries(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("a", "ellen", "s1", "m1")
        await reg.register("b", "tina", "s2", "m2")

        entries = reg.all_entries()
        assert set(entries.keys()) == {"a", "b"}
