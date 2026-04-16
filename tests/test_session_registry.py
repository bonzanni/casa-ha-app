"""Tests for session_registry.py."""

import time

import pytest

from session_registry import SessionRegistry


class TestSessionRegistry:
    def test_register_and_get(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        reg.register("tg:123", "ellen", "sdk-1", "mem-1")

        entry = reg.get("tg:123")
        assert entry is not None
        assert entry["agent"] == "ellen"
        assert entry["sdk_session_id"] == "sdk-1"
        assert entry["memory_session_id"] == "mem-1"
        assert "last_active" in entry

    def test_get_missing_returns_none(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        assert reg.get("nonexistent") is None

    def test_touch_updates_last_active(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        reg.register("tg:123", "ellen", "sdk-1", "mem-1")

        ts1 = reg.get("tg:123")["last_active"]
        time.sleep(0.05)
        reg.touch("tg:123")
        ts2 = reg.get("tg:123")["last_active"]

        assert ts2 >= ts1

    def test_persistence(self, tmp_path):
        """Data survives across two instances."""
        path = str(tmp_path / "sessions.json")

        r1 = SessionRegistry(path)
        r1.register("tg:42", "tina", "sdk-2", "mem-2")

        r2 = SessionRegistry(path)
        entry = r2.get("tg:42")
        assert entry is not None
        assert entry["agent"] == "tina"

    def test_remove(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        reg.register("tg:99", "ellen", "sdk-3", "mem-3")
        assert reg.get("tg:99") is not None

        reg.remove("tg:99")
        assert reg.get("tg:99") is None

    def test_all_entries(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        reg.register("a", "ellen", "s1", "m1")
        reg.register("b", "tina", "s2", "m2")

        entries = reg.all_entries()
        assert set(entries.keys()) == {"a", "b"}
