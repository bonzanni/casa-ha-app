"""D-4 auto-reap: stale engagements are cancelled through the finalize funnel.

Interrupted/abandoned engagements used to linger `active` forever (25-day
stale engagement found 2026-07-10; restart-orphan reaped manually
2026-07-11). ``tools.reap_stale_engagements`` runs in the daily engagement
sweep, BEFORE the idle-reminder pass, so a to-be-reaped record doesn't get a
pointless reminder in the same run.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _wire(tmp_path):
    from engagement_registry import EngagementRegistry
    from tools import init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()
    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    driver = MagicMock()
    driver.cancel = AsyncMock()
    return reg, telegram, bus, driver


async def _stale_record(reg, *, days: float, topic_id: int = 42, status: str = "active"):
    rec = await reg.create(
        "executor", "configurator", "claude_code", "install plugin X",
        {"role": "assistant", "channel": "telegram", "chat_id": "123"}, topic_id,
    )
    rec.started_at = time.time() - days * 86400
    rec.last_user_turn_ts = time.time() - days * 86400
    if status != "active":
        rec.status = status
    return rec


class TestReapStaleEngagements:
    async def test_reaps_active_past_ttl(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=8)
        await reap_stale_engagements(driver=driver, ttl_days=7)
        assert rec.status == "cancelled"
        telegram.close_topic.assert_awaited_once_with(thread_id=42)
        bus.notify.assert_awaited_once()

    async def test_reaps_idle_past_ttl(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=9, status="idle")
        await reap_stale_engagements(driver=driver, ttl_days=7)
        assert rec.status == "cancelled"

    async def test_skips_young_records(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=2)
        await reap_stale_engagements(driver=driver, ttl_days=7)
        assert rec.status == "active"
        telegram.close_topic.assert_not_awaited()

    async def test_ttl_zero_disables_reap(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=100)
        await reap_stale_engagements(driver=driver, ttl_days=0)
        assert rec.status == "active"

    async def test_ttl_defaults_from_env(self, tmp_path, monkeypatch):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        rec = await _stale_record(reg, days=3)
        monkeypatch.setenv("ENGAGEMENT_REAP_DAYS", "2")
        await reap_stale_engagements(driver=driver)
        assert rec.status == "cancelled"

    async def test_garbage_env_falls_back_to_default_7d(self, tmp_path, monkeypatch):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        young = await _stale_record(reg, days=3, topic_id=42)
        old = await _stale_record(reg, days=8, topic_id=43)
        monkeypatch.setenv("ENGAGEMENT_REAP_DAYS", "banana")
        await reap_stale_engagements(driver=driver)
        assert young.status == "active"
        assert old.status == "cancelled"

    async def test_one_bad_record_does_not_stop_the_sweep(self, tmp_path):
        from tools import reap_stale_engagements

        reg, telegram, bus, driver = _wire(tmp_path)
        bad = await _stale_record(reg, days=10, topic_id=44)
        good = await _stale_record(reg, days=10, topic_id=45)
        # First close_topic explodes; the second record must still be reaped.
        telegram.close_topic.side_effect = [RuntimeError("tg down"), None]
        await reap_stale_engagements(driver=driver, ttl_days=7)
        assert bad.status == "cancelled"   # registry transition precedes channel I/O
        assert good.status == "cancelled"
