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


class TestMigration:
    async def test_legacy_memory_session_id_field_is_dropped_on_rewrite(
        self, tmp_path,
    ):
        import json
        path = tmp_path / "sessions.json"
        path.write_text(json.dumps({
            "telegram:1": {
                "agent": "assistant",
                "sdk_session_id": "sdk-legacy",
                "memory_session_id": "mem-legacy",
                "last_active": "2026-04-01T00:00:00+00:00",
            }
        }))

        reg = SessionRegistry(str(path))
        # Load-time preservation is fine; write-through drops the field.
        await reg.touch("telegram:1")

        data = json.loads(path.read_text())
        assert "memory_session_id" not in data["telegram:1"]
        assert data["telegram:1"]["sdk_session_id"] == "sdk-legacy"


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
