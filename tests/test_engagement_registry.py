"""Tests for engagement_registry.py — engagement lifecycle + tombstone."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


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
    async def test_mark_completed_persists_terminal_tombstone(self, tmp_path):
        """D-4: terminal records STAY on disk as tombstones (they used to be
        dropped, so the duplicate-task guard forgot them across restarts and
        the file never matched the 'tombstone' name)."""
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        await reg.mark_completed(rec.id, completed_at=time.time())
        assert rec.status == "completed"
        rows = json.loads((tmp_path / "e.json").read_text())
        assert [r["id"] for r in rows] == [rec.id]
        assert rows[0]["status"] == "completed"
        # Still in memory for short-term lookups (by_topic_id after /cancel)
        assert reg.get(rec.id) is rec

    async def test_terminal_tombstones_pruned_after_retention(self, tmp_path):
        """D-4: terminal tombstones age out of the file (30d) so it can't
        grow unboundedly; in-flight records are never pruned."""
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        old = await reg.create("executor", "configurator", "claude_code", "t1", {}, 1)
        await reg.mark_completed(old.id, completed_at=time.time() - 31 * 86400)
        fresh = await reg.create("specialist", "finance", "in_casa", "t2", {}, 2)
        rows = json.loads((tmp_path / "e.json").read_text())
        ids = [r["id"] for r in rows]
        assert fresh.id in ids
        assert old.id not in ids, "31d-old terminal tombstone must be pruned"

    async def test_load_reconciles_active_to_idle(self, tmp_path):
        """D-4 boot reconcile: a record loaded as 'active' cannot have a live
        driver (the process that ran it died with the old container) — load()
        must flip it to idle so it stops claiming to run forever."""
        from engagement_registry import EngagementRegistry

        base = {
            "kind": "executor", "role_or_type": "configurator",
            "driver": "claude_code", "started_at": 1000.0,
            "last_user_turn_ts": 1000.0, "origin": {}, "task": "t",
        }
        tombstone = tmp_path / "engagements.json"
        tombstone.write_text(json.dumps([
            {**base, "id": "e-active", "status": "active", "topic_id": 1},
            {**base, "id": "e-idle", "status": "idle", "topic_id": 2},
            # recent completed_at so the 30d tombstone-prune keeps it.
            {**base, "id": "e-done", "status": "completed", "topic_id": 3,
             "completed_at": time.time()},
        ]))
        reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        await reg.load()
        assert reg.get("e-active").status == "idle"
        assert reg.get("e-idle").status == "idle"
        assert reg.get("e-done").status == "completed"
        assert {r.id for r in reg.active_and_idle()} == {"e-active", "e-idle"}
        # v0.69.6: the reconcile must be PERSISTED — the disk-reading auditor
        # (invariant E) must not keep seeing the stale "active" until some
        # later mutation happens to rewrite the file.
        on_disk = {r["id"]: r["status"] for r in json.loads(tombstone.read_text())}
        assert on_disk["e-active"] == "idle", "boot reconcile must persist to disk"
        assert on_disk["e-idle"] == "idle"
        assert on_disk["e-done"] == "completed"

    async def test_load_without_reconcile_does_not_rewrite(self, tmp_path):
        """No active records → nothing to reconcile → load() must not rewrite
        the file (avoid needless boot churn / mtime bump)."""
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        tombstone.write_text(json.dumps([{
            "id": "e-idle", "kind": "specialist", "role_or_type": "finance",
            "driver": "in_casa", "status": "idle", "topic_id": 2,
            "started_at": 1000.0, "last_user_turn_ts": 1000.0,
            "origin": {}, "task": "t",
        }]))
        before = tombstone.read_text()
        reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        await reg.load()
        assert tombstone.read_text() == before, "load() rewrote with nothing to reconcile"

    async def test_recent_for_origin_survives_restart_via_tombstone(self, tmp_path):
        """D-4: the P32 duplicate-task guard reads recent engagements from
        memory; persisting terminal tombstones makes it hold across restarts."""
        from engagement_registry import EngagementRegistry

        path = str(tmp_path / "e.json")
        reg1 = EngagementRegistry(tombstone_path=path, bus=None)
        rec = await reg1.create(
            "executor", "configurator", "claude_code", "install plugin X",
            {"channel": "telegram", "chat_id": "123"}, 1,
        )
        await reg1.mark_completed(rec.id, completed_at=time.time())

        reg2 = EngagementRegistry(tombstone_path=path, bus=None)
        await reg2.load()
        found = reg2.recent_for_origin(
            channel="telegram", chat_id="123", max_age_s=3600,
        )
        assert found is not None and found.id == rec.id

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

    async def test_try_transition_terminal_wins_once(self, tmp_path):
        """L75/L24: try_transition_terminal is the atomic gate — the first
        caller wins and flips the record; every subsequent caller (even
        with a different outcome) is refused and the status is untouched."""
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)

        won = await reg.try_transition_terminal(rec.id, "cancelled")
        assert won is True
        assert rec.status == "cancelled"

        # A second caller (e.g. emit_completion resuming after the race)
        # must NOT win and must NOT overwrite the winning outcome.
        won2 = await reg.try_transition_terminal(rec.id, "completed", completed_at=999.0)
        assert won2 is False
        assert rec.status == "cancelled"

    async def test_try_transition_terminal_missing_record_returns_false(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        assert await reg.try_transition_terminal("ghost", "completed") is False

    async def test_try_transition_terminal_error_sets_origin(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        won = await reg.try_transition_terminal(
            rec.id, "error", error_kind="emit_completion_error", error_message="boom",
        )
        assert won is True
        assert rec.status == "error"
        assert rec.origin.get("error_kind") == "emit_completion_error"
        assert rec.origin.get("error_message") == "boom"

    async def test_strict_transition_rolls_back_full_field_on_persist_failure(
        self, tmp_path,
    ):
        """v0.79.0 (§3, Sol r7-2): the STRICT terminal transition snapshots
        EVERY mutated field (status, completed_at, error metadata) and, on a
        tombstone-write failure, restores the FULL snapshot and re-raises — no
        closed topic with a torn (memory ≠ disk) terminal record."""
        import engagement_registry as er_mod
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        # Baseline: an active record with no error metadata.
        assert rec.status == "active"
        assert "error_kind" not in rec.origin

        # Fail the tombstone write only for the strict transition.
        import unittest.mock as _mock
        with _mock.patch.object(
            reg, "_write_tombstone", side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError):
                await reg.try_transition_terminal(
                    rec.id, "error", error_kind="k", error_message="m",
                    strict=True,
                )
        # FULL-FIELD rollback: status, completed_at AND the error metadata that
        # the transition added are all reverted (the added keys are removed,
        # not left as None).
        assert rec.status == "active"
        assert rec.completed_at is None
        assert "error_kind" not in rec.origin
        assert "error_message" not in rec.origin

    async def test_strict_transition_cancel_during_persist_completes_write(
        self, tmp_path,
    ):
        """v0.79.0 (§3, Sol r7-2): cancelling the CALLER during the strict
        transition's ``to_thread`` persist cannot tear memory from disk — the
        shielded mutate+persist runs to completion before the cancel is honored.
        """
        import asyncio
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)

        async def _driver():
            await reg.try_transition_terminal(rec.id, "completed", strict=True)

        task = asyncio.ensure_future(_driver())
        await asyncio.sleep(0)          # let it enter the shielded persist
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The durable write completed under the shield: memory is terminal AND
        # a fresh registry load sees the same terminal status.
        assert rec.status == "completed"
        reg2 = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        assert reg2.get(rec.id).status == "completed"

    async def test_non_strict_transition_swallows_persist_failure(self, tmp_path):
        """The historical non-strict path keeps best-effort semantics: a
        tombstone-write failure is swallowed and the in-memory flip stands."""
        import unittest.mock as _mock
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        with _mock.patch.object(
            reg, "_write_tombstone", side_effect=OSError("disk full"),
        ):
            won = await reg.try_transition_terminal(rec.id, "cancelled")
        assert won is True
        assert rec.status == "cancelled"

    async def test_terminal_records_accessor(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        live = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        gone = await reg.create("specialist", "ops", "claude_code", "t", {}, 2)
        await reg.try_transition_terminal(gone.id, "completed")
        ids = {r.id for r in reg.terminal_records()}
        assert gone.id in ids and live.id not in ids

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

    async def test_tombstone_round_trip(self, tmp_path, bus):
        from engagement_registry import EngagementRegistry
        path = str(tmp_path / "engagements.json")
        reg1 = EngagementRegistry(tombstone_path=path, bus=bus)
        await reg1.load()
        await reg1.create(
            kind="executor",
            role_or_type="plugin-developer",
            driver="claude_code",
            task="t",
            origin={},
            topic_id=None,
            tools_allowed=("Bash(npm*)", "Read"),
        )
        # Fresh registry reading the same tombstone:
        reg2 = EngagementRegistry(tombstone_path=path, bus=bus)
        await reg2.load()
        ids = list(reg2._records)
        assert len(ids) == 1
        rec = reg2._records[ids[0]]
        assert rec.tools_allowed == ("Bash(npm*)", "Read")

    async def test_pre_v0_37_2_tombstone_loads_with_empty(self, tmp_path, bus):
        """Back-compat: tombstones written before v0.37.2 lack the field."""
        import json
        from engagement_registry import EngagementRegistry
        path = tmp_path / "engagements.json"
        path.write_text(json.dumps([{
            "id": "a" * 32,
            "kind": "executor",
            "role_or_type": "plugin-developer",
            "driver": "claude_code",
            "status": "active",
            "topic_id": 42,
            "started_at": 1700000000.0,
            "last_user_turn_ts": 1700000000.0,
            "last_idle_reminder_ts": 0.0,
            "completed_at": None,
            "sdk_session_id": None,
            "origin": {},
            "task": "legacy",
        }]))
        reg = EngagementRegistry(tombstone_path=str(path), bus=bus)
        await reg.load()
        rec = reg._records["a" * 32]
        assert rec.tools_allowed == ()


class TestRecentForOrigin:
    """P32 (v0.37.10): query the most-recent engagement for a given
    (channel, chat_id) within a time window. Powers the duplicate-task
    guard at the engage_executor MCP call site — see
    docs/bug-review-2026-05-14-exploration6.md::O-6.
    """

    async def test_returns_most_recent_within_window(self, registry):
        older = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="old", origin={"channel": "telegram", "chat_id": "c1"},
            topic_id=10,
        )
        newer = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="new", origin={"channel": "telegram", "chat_id": "c1"},
            topic_id=11,
        )
        result = registry.recent_for_origin(
            channel="telegram", chat_id="c1", max_age_s=60.0,
        )
        assert result is newer
        assert result.id != older.id

    async def test_returns_none_for_no_match(self, registry):
        await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"channel": "telegram", "chat_id": "c1"},
            topic_id=10,
        )
        assert registry.recent_for_origin(
            channel="telegram", chat_id="other", max_age_s=60.0,
        ) is None
        assert registry.recent_for_origin(
            channel="discord", chat_id="c1", max_age_s=60.0,
        ) is None

    async def test_excludes_older_than_max_age(self, registry):
        import time as time_mod
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"channel": "telegram", "chat_id": "c1"},
            topic_id=10,
        )
        # Backdate the engagement past the window.
        rec.started_at = time_mod.time() - 300.0
        assert registry.recent_for_origin(
            channel="telegram", chat_id="c1", max_age_s=60.0,
        ) is None
        # But a wider window still returns it.
        assert registry.recent_for_origin(
            channel="telegram", chat_id="c1", max_age_s=600.0,
        ) is rec

    async def test_includes_terminal_status_engagements(self, registry):
        """Completed / cancelled / error engagements stay in memory
        post-finalize (the tombstone drops them but ``_records`` retains).
        The duplicate-task guard must still see them so back-to-back
        spawns where the prior just terminated are caught."""
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"channel": "telegram", "chat_id": "c1"},
            topic_id=10,
        )
        await registry.mark_completed(rec.id, completed_at=rec.started_at + 1)
        # Even completed, recent_for_origin returns it for the duplicate
        # guard (we want to compare against the LAST task spawned, not the
        # last ACTIVE task).
        assert registry.recent_for_origin(
            channel="telegram", chat_id="c1", max_age_s=60.0,
        ) is rec

    async def test_coerces_chat_id_to_string(self, registry):
        """``EngagementRecord.origin['chat_id']`` may be int or str
        depending on the channel adapter. The query must coerce for a
        consistent compare so a telegram int chat_id matches a str."""
        await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"channel": "telegram", "chat_id": 42},
            topic_id=10,
        )
        assert registry.recent_for_origin(
            channel="telegram", chat_id="42", max_age_s=60.0,
        ) is not None


# ---------------------------------------------------------------------------
# TestTombstoneAtomicity — M15: crash mid-write must not corrupt the tombstone
# ---------------------------------------------------------------------------


class TestTombstoneAtomicity:
    async def test_crash_between_tempwrite_and_replace_keeps_tombstone(
        self, tmp_path, monkeypatch,
    ):
        """A crash BETWEEN the temp write and os.replace must leave the prior
        engagements.json intact (not truncated), preserving in-flight state."""
        import atomic_io
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="keep me", origin={"channel": "telegram", "chat_id": 1},
            topic_id=7,
        )
        first = json.loads(tombstone.read_text(encoding="utf-8"))
        assert first[0]["id"] == rec.id

        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash before replace")

        monkeypatch.setattr(atomic_io.os, "replace", boom)
        # _write_tombstone_locked swallows the exception (logs a warning), so
        # the mutation call itself must not raise — but disk must be untouched.
        await reg.update_user_turn(rec.id, ts=time.time())

        on_disk = json.loads(tombstone.read_text(encoding="utf-8"))
        assert on_disk == first  # prior tombstone intact, not truncated
        import os as _os
        assert [f for f in _os.listdir(tmp_path) if f != "engagements.json"] == []


# ---------------------------------------------------------------------------
# TestInteractionState — W2/Sol B9 (Task 7): observational turn-taking.
# ---------------------------------------------------------------------------


class TestInteractionState:
    async def test_default_is_empty_string(self, registry):
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="claude_code",
            task="t", origin={}, topic_id=1,
        )
        assert rec.interaction_state == ""

    @pytest.mark.parametrize("current,event,expected", [
        # first_contact: only valid from first_contact_required.
        ("first_contact_required", "first_contact", "awaiting_operator"),
        ("awaiting_operator", "first_contact", None),
        ("authorized", "first_contact", None),
        ("", "first_contact", None),
        # operator_answered / operator_turn: valid from EITHER
        # pre-authorized state (r3-B4 — a fast tap beats the agent's
        # first reply), never backwards, never from "" or "authorized".
        ("first_contact_required", "operator_answered", "authorized"),
        ("awaiting_operator", "operator_answered", "authorized"),
        ("authorized", "operator_answered", None),
        ("", "operator_answered", None),
        ("first_contact_required", "operator_turn", "authorized"),
        ("awaiting_operator", "operator_turn", "authorized"),
        ("authorized", "operator_turn", None),
        ("", "operator_turn", None),
        # Unknown event is always a no-op.
        ("first_contact_required", "bogus_event", None),
        ("awaiting_operator", "bogus_event", None),
    ])
    async def test_transition_table(self, registry, current, event, expected):
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="claude_code",
            task="t", origin={}, topic_id=1,
        )
        rec.interaction_state = current
        result = await registry.advance_interaction_state(rec.id, event)
        assert result == expected
        assert rec.interaction_state == (expected if expected is not None else current)

    async def test_unknown_engagement_returns_none(self, registry):
        assert await registry.advance_interaction_state(
            "ghost", "first_contact",
        ) is None

    async def test_atomicity_concurrent_calls_resolve_to_one_transition(self, registry):
        """Two coroutines race the SAME event on the SAME record: the lock
        serializes read-compute-write, so exactly one call sees the
        pre-transition state (and wins) while the other sees the
        already-advanced state (and no-ops)."""
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="claude_code",
            task="t", origin={}, topic_id=1,
        )
        rec.interaction_state = "awaiting_operator"
        results = await asyncio.gather(
            registry.advance_interaction_state(rec.id, "operator_answered"),
            registry.advance_interaction_state(rec.id, "operator_answered"),
        )
        assert results.count("authorized") == 1
        assert results.count(None) == 1
        assert rec.interaction_state == "authorized"

    async def test_advance_raises_and_rolls_back_on_persist_failure(
        self, registry, monkeypatch,
    ):
        """B3 (Sol r1): advance_interaction_state persists STRICTLY — a REAL
        tombstone-write failure (the underlying file write raises, not the
        method) must propagate AND roll the in-memory field back to its prior
        value, so a restart never restores stale ``awaiting_operator`` after
        the callback thinks it authorized."""
        import engagement_registry as er

        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="claude_code",
            task="t", origin={}, topic_id=1,
        )
        rec.interaction_state = "first_contact_required"

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(er, "atomic_write_json", _boom)

        with pytest.raises(OSError):
            await registry.advance_interaction_state(rec.id, "operator_answered")

        # Rolled back: authorization never reached disk, so the in-memory
        # field must NOT be left advanced.
        assert rec.interaction_state == "first_contact_required"

    async def test_persists_across_tombstone_round_trip(self, tmp_path, bus):
        from engagement_registry import EngagementRegistry

        path = str(tmp_path / "engagements.json")
        reg1 = EngagementRegistry(tombstone_path=path, bus=bus)
        rec = await reg1.create(
            kind="executor", role_or_type="configurator", driver="claude_code",
            task="t", origin={}, topic_id=1,
        )
        rec.interaction_state = "first_contact_required"
        result = await reg1.advance_interaction_state(rec.id, "first_contact")
        assert result == "awaiting_operator"

        reg2 = EngagementRegistry(tombstone_path=path, bus=bus)
        await reg2.load()
        rec2 = reg2.get(rec.id)
        assert rec2 is not None
        assert rec2.interaction_state == "awaiting_operator"

    async def test_pre_v0_75_0_tombstone_loads_with_empty_default(self, tmp_path, bus):
        """Back-compat: tombstones written before Task 7 lack the field."""
        from engagement_registry import EngagementRegistry

        path = tmp_path / "engagements.json"
        path.write_text(json.dumps([{
            "id": "b" * 32,
            "kind": "executor",
            "role_or_type": "configurator",
            "driver": "claude_code",
            "status": "active",
            "topic_id": 42,
            "started_at": 1700000000.0,
            "last_user_turn_ts": 1700000000.0,
            "last_idle_reminder_ts": 0.0,
            "completed_at": None,
            "sdk_session_id": None,
            "origin": {},
            "task": "legacy",
        }]))
        reg = EngagementRegistry(tombstone_path=str(path), bus=bus)
        await reg.load()
        rec = reg._records["b" * 32]
        assert rec.interaction_state == ""


class TestSetInteractionViolated:
    async def test_sets_origin_flag(self, registry):
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="claude_code",
            task="t", origin={}, topic_id=1,
        )
        assert rec.origin.get("interaction_violated") is None
        await registry.set_interaction_violated(rec.id)
        assert rec.origin.get("interaction_violated") is True

    async def test_unknown_engagement_is_noop(self, registry):
        await registry.set_interaction_violated("ghost")  # must not raise

    async def test_persists_through_tombstone(self, tmp_path, bus):
        from engagement_registry import EngagementRegistry

        path = str(tmp_path / "engagements.json")
        reg1 = EngagementRegistry(tombstone_path=path, bus=bus)
        rec = await reg1.create(
            kind="executor", role_or_type="configurator", driver="claude_code",
            task="t", origin={}, topic_id=1,
        )
        await reg1.set_interaction_violated(rec.id)

        reg2 = EngagementRegistry(tombstone_path=path, bus=bus)
        await reg2.load()
        rec2 = reg2.get(rec.id)
        assert rec2 is not None
        assert rec2.origin.get("interaction_violated") is True

    async def test_raises_and_rolls_back_on_persist_failure(
        self, registry, monkeypatch,
    ):
        """B3 (Sol diff r2): set_interaction_violated persists STRICTLY — a
        REAL tombstone-write failure must PROPAGATE and roll the in-memory
        origin flag back, so the driver seam (which only marks
        ``_violation_flagged`` after a successful return) retries next frame
        instead of permanently losing the completion warning across a
        restart."""
        import engagement_registry as er

        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="claude_code",
            task="t", origin={}, topic_id=1,
        )
        assert rec.origin.get("interaction_violated") is None

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(er, "atomic_write_json", _boom)

        with pytest.raises(OSError):
            await registry.set_interaction_violated(rec.id)

        # Rolled back: the flag never reached disk, so the in-memory origin
        # must NOT be left set (else a restart would lose the un-persisted flag
        # silently while the driver believed it succeeded).
        assert rec.origin.get("interaction_violated") is None


# ---------------------------------------------------------------------------
# v0.79.0 (§4) — persisted question numbering + open-question ledger
# ---------------------------------------------------------------------------


class TestQuestionNumbering:
    async def test_allocate_is_monotonic_and_persisted(self, tmp_path):
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        rec = await reg.create("executor", "configurator", "claude_code", "t", {}, 1)

        assert await reg.allocate_question_number(rec.id) == 1
        assert await reg.allocate_question_number(rec.id) == 2
        assert await reg.allocate_question_number(rec.id) == 3
        assert rec.next_question_number == 4

        # Survives a reload: numbering is NEVER rewound.
        reg2 = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        await reg2.load()
        assert await reg2.allocate_question_number(rec.id) == 4

    async def test_open_question_ledger_add_close_accessor(self, tmp_path):
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        rec = await reg.create("executor", "configurator", "claude_code", "t", {}, 1)

        n1 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n1, 5001)
        n2 = await reg.allocate_question_number(rec.id)
        await reg.add_open_question(rec.id, n2, 5002)
        assert reg.open_question_numbers(rec.id) == [1, 2]

        await reg.close_open_question(rec.id, n1)
        assert reg.open_question_numbers(rec.id) == [2]

        # open_questions survive reload; next_question_number preserved.
        reg2 = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        await reg2.load()
        assert reg2.open_question_numbers(rec.id) == [2]
        assert reg2.get(rec.id).open_questions[0]["tg_message_id"] == 5002
        assert await reg2.allocate_question_number(rec.id) == 3

    async def test_add_open_question_idempotent_on_number(self, tmp_path):
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        rec = await reg.create("executor", "configurator", "claude_code", "t", {}, 1)
        await reg.add_open_question(rec.id, 1, 100)
        await reg.add_open_question(rec.id, 1, 200)  # same number → update, no dup
        assert reg.open_question_numbers(rec.id) == [1]
        assert rec.open_questions[0]["tg_message_id"] == 200

    async def test_allocate_rolls_back_on_persist_failure(self, tmp_path, monkeypatch):
        import engagement_registry as er
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
        rec = await reg.create("executor", "configurator", "claude_code", "t", {}, 1)

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(er, "atomic_write_json", _boom)
        with pytest.raises(OSError):
            await reg.allocate_question_number(rec.id)
        # Rolled back — the number never reached disk, so it must not be consumed.
        assert rec.next_question_number == 1


class TestSummaryState:
    """v0.79.0 (§5): summary_message_id persistence + monotonic revision."""

    async def test_set_summary_message_id_persists_and_reloads(self, tmp_path):
        from engagement_registry import EngagementRegistry

        path = str(tmp_path / "e.json")
        reg = EngagementRegistry(tombstone_path=path, bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="hello", driver="claude_code",
            task="t", origin={}, topic_id=5,
        )
        await reg.set_summary_message_id(rec.id, 4242)
        assert reg.get(rec.id).summary_message_id == 4242
        # Reload from disk.
        reg2 = EngagementRegistry(tombstone_path=path, bus=None)
        await reg2.load()
        assert reg2.get(rec.id).summary_message_id == 4242

    async def test_allocate_summary_revision_is_monotonic(self, tmp_path):
        from engagement_registry import EngagementRegistry

        path = str(tmp_path / "e.json")
        reg = EngagementRegistry(tombstone_path=path, bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="hello", driver="claude_code",
            task="t", origin={}, topic_id=5,
        )
        assert await reg.allocate_summary_revision(rec.id) == 0
        assert await reg.allocate_summary_revision(rec.id) == 1
        assert await reg.allocate_summary_revision(rec.id) == 2
        assert reg.get(rec.id).summary_revision == 3
        # Survives a reload (never rewound).
        reg2 = EngagementRegistry(tombstone_path=path, bus=None)
        await reg2.load()
        assert reg2.get(rec.id).summary_revision == 3
        assert await reg2.allocate_summary_revision(rec.id) == 3

    async def test_allocate_unknown_engagement_returns_none(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        assert await reg.allocate_summary_revision("nope") is None
        await reg.set_summary_message_id("nope", 1)  # no-op, no raise
