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
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV

pytestmark = [pytest.mark.unit]


async def test_sweep_saves_only_cold_conversational_entries(tmp_path):
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    # cold voice (idle 1h > 30m window) → REMOVED (recall-only, not saved)
    await reg.register("voice-r1", "assistant", "sid-1", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["voice-r1"]["last_active"] = (now - timedelta(hours=1)).isoformat()
    # cold telegram (idle 13h > 12h window) → saved
    await reg.register("telegram-r2", "assistant", "sid-3", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["telegram-r2"]["last_active"] = (now - timedelta(hours=13)).isoformat()
    # warm telegram (idle 1h < 12h window) → skip
    await reg.register("telegram-42", "assistant", "sid-2", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["telegram-42"]["last_active"] = (now - timedelta(hours=1)).isoformat()

    saved = []
    async def fake_save(key, *a, **k):
        saved.append(key); return True

    reaper = FreshnessReaper(
        registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda role: f"/home/{role}", now=lambda: now, save_fn=fake_save,
    )
    await reaper.sweep_once()
    assert saved == ["telegram-r2"]          # cold telegram saved
    assert reg.get("voice-r1") is None       # cold voice entry removed


async def test_sweep_skips_webhook_and_scheduler(tmp_path):
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    await reg.register("webhook-abc", "assistant", "sid-3", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
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
    # cold voice with a FRESH in-flight claim → skip (a save is running); claim left intact
    await reg.register("voice-fresh", "assistant", "sid-a", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["voice-fresh"]["last_active"] = (now - timedelta(hours=1)).isoformat()
    reg._data["voice-fresh"]["consolidated_at"] = (now - timedelta(minutes=1)).isoformat()
    # cold telegram with a STALE claim (crashed mid-save) → recover + save
    await reg.register("telegram-stale", "assistant", "sid-b", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["telegram-stale"]["last_active"] = (now - timedelta(hours=13)).isoformat()
    reg._data["telegram-stale"]["consolidated_at"] = (now - timedelta(hours=5)).isoformat()

    saved = []
    async def fake_save(key, *a, **k):
        saved.append(key); return True
    reaper = FreshnessReaper(registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda r: "/h", now=lambda: now, save_fn=fake_save,
        interval_s=3600.0)
    await reaper.sweep_once()
    assert saved == ["telegram-stale"]                          # stale claim recovered + saved
    assert "consolidated_at" not in reg.get("telegram-stale")  # clear_save_claim ran before save_fn; entry remains because the fake save_fn doesn't finish_save
    assert reg.get("voice-fresh").get("consolidated_at")        # fresh claim untouched


async def test_sweep_drops_stale_legacy_entry_with_no_provenance(tmp_path):
    """M1: a legacy pre-Task-9 entry (valid agent/sdk_session_id, but no
    speaker_provenance/user_provenance) must be DROPPED like a None snapshot,
    not handed to save_session forever (save_session refuses to retain it and
    returns False without removing the entry, which would churn every sweep)."""
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    await reg.register("telegram-legacy", "assistant", "sid-legacy", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    # Simulate a pre-Task-9 entry: strip the provenance fields register() added.
    del reg._data["telegram-legacy"]["speaker_provenance"]
    del reg._data["telegram-legacy"]["user_provenance"]
    reg._data["telegram-legacy"]["last_active"] = (now - timedelta(hours=13)).isoformat()

    save_fn = AsyncMock(return_value=True)
    reaper = FreshnessReaper(
        registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda role: f"/home/{role}", now=lambda: now, save_fn=save_fn,
    )
    await reaper.sweep_once()
    assert save_fn.await_count == 0            # never handed to a save that can't retain it
    assert reg.get("telegram-legacy") is None  # stale pointer dropped, not retried forever


def test_is_stale_claim_handles_bad_input():
    from datetime import datetime, timezone
    reaper = FreshnessReaper(registry=None, semantic_memory=None,
        directory_for=lambda r: "/h", interval_s=3600.0)
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    assert reaper._is_stale_claim(None, now) is True          # non-str → reclaim
    assert reaper._is_stale_claim("not-a-date", now) is True  # bad ISO → reclaim
