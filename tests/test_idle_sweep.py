"""Tests for engagement_registry.sweep_idle_and_suspend."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


class TestSweep:
    async def test_fires_idle_detected_after_3_days_for_specialist(self, tmp_path):
        from engagement_registry import EngagementRegistry

        bus = MagicMock(); bus.notify = AsyncMock()
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=bus)
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               {"role": "assistant"}, topic_id=1)
        # Backdate last user turn by 4 days
        rec.last_user_turn_ts -= 4 * 86400
        driver = MagicMock()
        driver.is_alive = MagicMock(return_value=False)

        await reg.sweep_idle_and_suspend(driver=driver, now_override=rec.last_user_turn_ts + 4*86400)
        assert bus.notify.await_count == 1
        # Reminder ts updated
        assert rec.last_idle_reminder_ts > 0

    async def test_does_not_refire_within_week(self, tmp_path):
        from engagement_registry import EngagementRegistry

        bus = MagicMock(); bus.notify = AsyncMock()
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=bus)
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               {"role": "assistant"}, topic_id=1)
        now = rec.started_at + 10 * 86400
        rec.last_user_turn_ts = rec.started_at  # 10 days idle
        rec.last_idle_reminder_ts = now - 3 * 86400  # fired 3 days ago
        driver = MagicMock(); driver.is_alive = MagicMock(return_value=False)

        await reg.sweep_idle_and_suspend(driver=driver, now_override=now)
        bus.notify.assert_not_called()

    async def test_refires_after_week(self, tmp_path):
        from engagement_registry import EngagementRegistry

        bus = MagicMock(); bus.notify = AsyncMock()
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=bus)
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               {"role": "assistant"}, topic_id=1)
        now = rec.started_at + 14 * 86400
        rec.last_user_turn_ts = rec.started_at
        rec.last_idle_reminder_ts = now - 8 * 86400  # fired 8 days ago
        driver = MagicMock(); driver.is_alive = MagicMock(return_value=False)

        await reg.sweep_idle_and_suspend(driver=driver, now_override=now)
        bus.notify.assert_awaited_once()

    async def test_suspends_live_client_after_24h_idle(self, tmp_path):
        from engagement_registry import EngagementRegistry

        bus = MagicMock(); bus.notify = AsyncMock()
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=bus)
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               {"role": "assistant"}, topic_id=1)
        now = rec.started_at + 30 * 3600  # 30h later
        rec.last_user_turn_ts = rec.started_at
        driver = MagicMock()
        driver.is_alive = MagicMock(return_value=True)
        driver.get_session_id = MagicMock(return_value="sess-x")
        driver.cancel = AsyncMock()

        await reg.sweep_idle_and_suspend(driver=driver, now_override=now)
        driver.cancel.assert_awaited_once()
        assert rec.sdk_session_id == "sess-x"
        assert rec.status == "idle"

    async def test_skips_non_active_records(self, tmp_path):
        from engagement_registry import EngagementRegistry

        bus = MagicMock(); bus.notify = AsyncMock()
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=bus)
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               {"role": "assistant"}, topic_id=1)
        await reg.mark_completed(rec.id, completed_at=rec.started_at + 1)
        now = rec.started_at + 10 * 86400
        driver = MagicMock(); driver.is_alive = MagicMock(return_value=False)
        await reg.sweep_idle_and_suspend(driver=driver, now_override=now)
        bus.notify.assert_not_called()
