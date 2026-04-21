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
        await reg.register("tg:123", "assistant", "sdk-1")

        entry = reg.get("tg:123")
        assert entry is not None
        assert entry["agent"] == "assistant"
        assert entry["sdk_session_id"] == "sdk-1"
        assert "last_active" in entry

    async def test_get_missing_returns_none(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        assert reg.get("nonexistent") is None

    async def test_touch_updates_last_active(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("tg:123", "assistant", "sdk-1")

        ts1 = reg.get("tg:123")["last_active"]
        await asyncio.sleep(0.05)
        await reg.touch("tg:123")
        ts2 = reg.get("tg:123")["last_active"]

        assert ts2 >= ts1

    async def test_persistence(self, tmp_path):
        """Data survives across two instances."""
        path = str(tmp_path / "sessions.json")

        r1 = SessionRegistry(path)
        await r1.register("tg:42", "butler", "sdk-2")

        r2 = SessionRegistry(path)
        entry = r2.get("tg:42")
        assert entry is not None
        assert entry["agent"] == "butler"

    async def test_remove(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("tg:99", "assistant", "sdk-3")
        assert reg.get("tg:99") is not None

        await reg.remove("tg:99")
        assert reg.get("tg:99") is None

    async def test_all_entries(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("a", "assistant", "s1")
        await reg.register("b", "butler", "s2")

        entries = reg.all_entries()
        assert set(entries.keys()) == {"a", "b"}


class TestConcurrency:
    async def test_concurrent_register_on_distinct_keys_preserves_all(
        self, tmp_path,
    ):
        """50 concurrent register() calls, distinct keys, all persist."""
        import json

        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)

        async def reg_one(i: int) -> None:
            await reg.register(f"tg:{i}", "assistant", f"sdk-{i}")

        await asyncio.gather(*(reg_one(i) for i in range(50)))

        on_disk = json.loads((tmp_path / "sessions.json").read_text())
        assert len(on_disk) == 50
        for i in range(50):
            assert on_disk[f"tg:{i}"]["sdk_session_id"] == f"sdk-{i}"

    async def test_concurrent_register_and_touch_preserve_sdk_session_id(
        self, tmp_path,
    ):
        """register(new sdk_session_id) + touch() on same key: final state keeps sdk_session_id."""
        import json

        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        # Seed an entry so touch() has something to update.
        await reg.register("tg:1", "assistant", "sdk-OLD")

        # Now race: register overwrites with sdk-NEW, touch updates last_active.
        await asyncio.gather(
            reg.register("tg:1", "assistant", "sdk-NEW"),
            reg.touch("tg:1"),
        )

        on_disk = json.loads((tmp_path / "sessions.json").read_text())
        assert on_disk["tg:1"]["sdk_session_id"] == "sdk-NEW"

    async def test_public_save_acquires_lock_while_internal_save_assumes_held(
        self, tmp_path,
    ):
        """public save() must acquire; _save_locked() must not (caller holds)."""
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("tg:1", "assistant", "sdk-1")

        # Holding the lock, _save_locked must succeed without deadlock.
        async with reg._lock:
            await reg._save_locked()

        # Public save() acquires fresh (no caller holds) and completes.
        await reg.save()


# ---------------------------------------------------------------------------
# TestClearSdkSession — 5.8 §3.1
# ---------------------------------------------------------------------------


class TestClearSdkSession:
    """clear_sdk_session drops only the sdk_session_id field; keeps entry."""

    async def test_removes_sdk_session_id_field(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("voice:scope-a", "butler", "sid-123")
        assert reg.get("voice:scope-a")["sdk_session_id"] == "sid-123"

        await reg.clear_sdk_session("voice:scope-a")

        entry = reg.get("voice:scope-a")
        assert entry is not None
        assert "sdk_session_id" not in entry

    async def test_keeps_last_active_and_agent(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("voice:scope-a", "butler", "sid-123")
        before = reg.get("voice:scope-a")["last_active"]

        await reg.clear_sdk_session("voice:scope-a")

        entry = reg.get("voice:scope-a")
        assert entry["agent"] == "butler"
        assert entry["last_active"] == before

    async def test_missing_key_is_noop(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)

        # Must not raise.
        await reg.clear_sdk_session("voice:never-registered")

        assert reg.get("voice:never-registered") is None

    async def test_persists_to_disk(self, tmp_path):
        import json
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("voice:scope-a", "butler", "sid-123")

        await reg.clear_sdk_session("voice:scope-a")

        # Re-read fresh from disk.
        with open(path) as fh:
            data = json.load(fh)
        assert "voice:scope-a" in data
        assert "sdk_session_id" not in data["voice:scope-a"]
        assert data["voice:scope-a"]["agent"] == "butler"
