# tests/test_freshness_reaper.py
"""FreshnessReaper (spec §4.2 entry point 1): saves sessions idle past their
channel freshness window. Runs once at boot then hourly; never resumes a saved
session. Includes C3 stale-claim recovery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from freshness_reaper import FreshnessReaper
from session_registry import SessionRegistry

pytestmark = [pytest.mark.unit]


async def test_sweep_saves_only_cold_conversational_entries(tmp_path):
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    # cold voice (idle 1h > 30m window) → save
    await reg.register("voice-r1", "assistant", "sid-1")
    reg._data["voice-r1"]["last_active"] = (now - timedelta(hours=1)).isoformat()
    # warm telegram (idle 1h < 12h window) → skip
    await reg.register("telegram-42", "assistant", "sid-2")
    reg._data["telegram-42"]["last_active"] = (now - timedelta(hours=1)).isoformat()

    saved = []
    async def fake_save(key, *a, **k):
        saved.append(key); return True

    reaper = FreshnessReaper(
        registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda role: f"/home/{role}", now=lambda: now, save_fn=fake_save,
    )
    await reaper.sweep_once()
    assert saved == ["voice-r1"]


async def test_sweep_skips_webhook_and_scheduler(tmp_path):
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    await reg.register("webhook-abc", "assistant", "sid-3")
    reg._data["webhook-abc"]["last_active"] = (now - timedelta(days=5)).isoformat()
    saved = []
    async def fake_save(key, *a, **k):
        saved.append(key); return True
    reaper = FreshnessReaper(registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda r: "/h", now=lambda: now, save_fn=fake_save)
    await reaper.sweep_once()
    assert saved == []   # webhook one-shots are not retained


async def test_fresh_claim_is_skipped_but_stale_claim_is_recovered(tmp_path):
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    # cold voice with a FRESH in-flight claim → skip (a save is running)
    await reg.register("voice-fresh", "assistant", "sid-a")
    reg._data["voice-fresh"]["last_active"] = (now - timedelta(hours=1)).isoformat()
    reg._data["voice-fresh"]["consolidated_at"] = (now - timedelta(minutes=1)).isoformat()
    # cold voice with a STALE claim (crashed mid-save) → recover + save
    await reg.register("voice-stale", "assistant", "sid-b")
    reg._data["voice-stale"]["last_active"] = (now - timedelta(hours=5)).isoformat()
    reg._data["voice-stale"]["consolidated_at"] = (now - timedelta(hours=5)).isoformat()

    saved = []
    async def fake_save(key, *a, **k):
        saved.append(key); return True
    reaper = FreshnessReaper(registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda r: "/h", now=lambda: now, save_fn=fake_save,
        interval_s=3600.0)
    await reaper.sweep_once()
    assert saved == ["voice-stale"]                       # stale recovered + saved
    assert "consolidated_at" not in reg.get("voice-stale")  # clear_save_claim ran before save_fn; entry remains because the fake save_fn doesn't finish_save
    assert reg.get("voice-fresh").get("consolidated_at")    # fresh claim untouched


def test_is_stale_claim_handles_bad_input():
    from datetime import datetime, timezone
    reaper = FreshnessReaper(registry=None, semantic_memory=None,
        directory_for=lambda r: "/h", interval_s=3600.0)
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    assert reaper._is_stale_claim(None, now) is True          # non-str → reclaim
    assert reaper._is_stale_claim("not-a-date", now) is True  # bad ISO → reclaim
