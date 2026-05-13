"""Tests for engagement_registry.py — engagement lifecycle + tombstone."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


class TestEngagementRecord:
    def test_dataclass_shape(self):
        from engagement_registry import EngagementRecord

        rec = EngagementRecord(
            id="e1",
            kind="specialist",
            role_or_type="finance",
            driver="in_casa",
            status="active",
            topic_id=12345,
            started_at=1000.0,
            last_user_turn_ts=1000.0,
            last_idle_reminder_ts=0.0,
            completed_at=None,
            sdk_session_id=None,
            origin={"role": "assistant", "channel": "telegram"},
            task="Plan Q2 invoicing",
        )
        assert rec.id == "e1"
        assert rec.kind == "specialist"
        assert rec.topic_id == 12345
        assert rec.sdk_session_id is None
        assert rec.origin["role"] == "assistant"


class TestRegistryInitAndLoad:
    async def test_load_missing_tombstone_is_empty(self, tmp_path):
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        await reg.load()
        assert reg.active_and_idle() == []

    async def test_load_reads_active_records(self, tmp_path):
        from engagement_registry import EngagementRegistry, EngagementRecord

        tombstone = tmp_path / "engagements.json"
        tombstone.write_text(json.dumps([
            {
                "id": "e1",
                "kind": "specialist",
                "role_or_type": "finance",
                "driver": "in_casa",
                "status": "active",
                "topic_id": 42,
                "started_at": 1000.0,
                "last_user_turn_ts": 1000.0,
                "last_idle_reminder_ts": 0.0,
                "completed_at": None,
                "sdk_session_id": None,
                "origin": {"role": "assistant"},
                "task": "x",
            }
        ]))
        reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        await reg.load()
        records = reg.active_and_idle()
        assert len(records) == 1
        assert records[0].id == "e1"
        assert records[0].topic_id == 42

    async def test_load_corrupt_tombstone_truncates_and_returns_empty(self, tmp_path, caplog):
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        tombstone.write_text("{not json")
        reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        await reg.load()
        assert reg.active_and_idle() == []
        # File truncated to []
        assert json.loads(tombstone.read_text()) == []
        assert any("corrupt" in r.message.lower() for r in caplog.records)


class TestRegistryCreate:
    async def test_create_assigns_uuid_and_indexes_topic(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist",
            role_or_type="finance",
            driver="in_casa",
            task="task text",
            origin={"role": "assistant"},
            topic_id=777,
        )
        assert rec.id and len(rec.id) >= 32
        assert rec.status == "active"
        assert reg.get(rec.id) is rec
        assert reg.by_topic_id(777) is rec
        # Tombstone written
        assert (tmp_path / "e.json").exists()

    async def test_create_without_topic_id_still_writes_tombstone(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={}, topic_id=None,
        )
        assert rec.topic_id is None
        assert reg.by_topic_id(0) is None


class TestRegistryStateTransitions:
    async def test_mark_completed_drops_from_disk(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        await reg.mark_completed(rec.id, completed_at=2000.0)
        assert rec.status == "completed"
        assert rec.completed_at == 2000.0
        # Tombstone now empty on disk
        assert json.loads((tmp_path / "e.json").read_text()) == []
        # But still in memory for short-term lookups (by_topic_id after /cancel)
        assert reg.get(rec.id) is rec

    async def test_mark_idle_and_back_to_active(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        await reg.mark_idle(rec.id)
        assert rec.status == "idle"
        await reg.update_user_turn(rec.id, ts=3000.0)
        assert rec.status == "active"
        assert rec.last_user_turn_ts == 3000.0

    async def test_persist_session_id(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        await reg.persist_session_id(rec.id, "sess-abc")
        assert rec.sdk_session_id == "sess-abc"

    async def test_mark_error_captures_kind(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        await reg.mark_error(rec.id, kind="resume_failed", message="SDK rotated")
        assert rec.status == "error"
        assert rec.origin.get("error_kind") == "resume_failed"
        assert rec.origin.get("error_message") == "SDK rotated"

    async def test_update_last_idle_reminder(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        await reg.update_last_idle_reminder(rec.id, ts=5000.0)
        assert rec.last_idle_reminder_ts == 5000.0


class TestChannelStateFields:
    """E-12 (v0.37.0): EngagementRecord carries channel-side state."""

    async def test_record_has_channel_fields_with_defaults(self, tmp_path):
        from engagement_registry import EngagementRegistry
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"), bus=None,
        )
        rec = await reg.create(
            kind="executor", role_or_type="plugin-developer",
            driver="claude_code", task="t", origin={}, topic_id=42,
        )
        assert rec.pinned_message_id is None
        assert rec.progress_message_id is None
        assert rec.current_state_emoji is None

    async def test_set_channel_state_persists_each_field(self, tmp_path):
        from engagement_registry import EngagementRegistry
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"), bus=None,
        )
        rec = await reg.create(
            kind="executor", role_or_type="plugin-developer",
            driver="claude_code", task="t", origin={}, topic_id=42,
        )
        await reg.set_channel_state(
            rec.id, pinned_message_id=100, progress_message_id=101,
            current_state_emoji="🟢",
        )
        assert rec.pinned_message_id == 100
        assert rec.progress_message_id == 101
        assert rec.current_state_emoji == "🟢"

    async def test_set_channel_state_leaves_omitted_fields_untouched(self, tmp_path):
        from engagement_registry import EngagementRegistry
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"), bus=None,
        )
        rec = await reg.create(
            kind="executor", role_or_type="plugin-developer",
            driver="claude_code", task="t", origin={}, topic_id=42,
        )
        await reg.set_channel_state(rec.id, pinned_message_id=100)
        await reg.set_channel_state(rec.id, current_state_emoji="🟡")
        assert rec.pinned_message_id == 100
        assert rec.progress_message_id is None
        assert rec.current_state_emoji == "🟡"

    async def test_channel_fields_round_trip_through_tombstone(self, tmp_path):
        from engagement_registry import EngagementRegistry
        tombstone = tmp_path / "engagements.json"
        reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="plugin-developer",
            driver="claude_code", task="t", origin={}, topic_id=42,
        )
        await reg.set_channel_state(
            rec.id, pinned_message_id=42, progress_message_id=43,
            current_state_emoji="🟢",
        )

        reg2 = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        await reg2.load()
        rec2 = reg2.get(rec.id)
        assert rec2 is not None
        assert rec2.pinned_message_id == 42
        assert rec2.progress_message_id == 43
        assert rec2.current_state_emoji == "🟢"

    async def test_set_channel_state_unknown_engagement_noop(self, tmp_path):
        from engagement_registry import EngagementRegistry
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"), bus=None,
        )
        # Should not raise.
        await reg.set_channel_state("does-not-exist", pinned_message_id=1)


@pytest.fixture
def bus():
    return None  # registry tolerates None per its docstring


@pytest.fixture
def registry(tmp_path, bus):
    from engagement_registry import EngagementRegistry
    return EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=bus,
    )


class TestToolsAllowedField:
    async def test_default_empty_tuple(self, registry):
        rec = await registry.create(
            kind="executor",
            role_or_type="plugin-developer",
            driver="claude_code",
            task="probe",
            origin={},
            topic_id=None,
        )
        assert rec.tools_allowed == ()

    async def test_create_accepts_tools_allowed(self, registry):
        allow = ("Bash(npm*)", "Read", "Edit(/data/engagements/*)")
        rec = await registry.create(
            kind="executor",
            role_or_type="plugin-developer",
            driver="claude_code",
            task="probe",
            origin={},
            topic_id=None,
            tools_allowed=allow,
        )
        assert rec.tools_allowed == allow
