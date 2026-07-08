# tests/test_session_registry_save_fields.py
"""Registry save-support fields (spec §4.2): consolidated_at atomic guard
against the reaper/next-turn double-retain race."""
from __future__ import annotations

import pytest

from session_registry import SessionRegistry

pytestmark = [pytest.mark.unit]


@pytest.fixture
def reg(tmp_path):
    return SessionRegistry(str(tmp_path / "sessions.json"))


async def test_try_begin_save_is_once_only(reg):
    await reg.register("voice-room1", "assistant", "sid-1")
    assert await reg.try_begin_save("voice-room1") is True   # first claim wins
    assert await reg.try_begin_save("voice-room1") is False  # already claimed
    # marker is set so a crashed save can be detected/retried by the reaper
    assert reg.get("voice-room1")["consolidated_at"]


async def test_finish_save_removes_entry(reg):
    await reg.register("voice-room1", "assistant", "sid-1")
    await reg.try_begin_save("voice-room1")
    await reg.finish_save("voice-room1")
    assert reg.get("voice-room1") is None


async def test_try_begin_save_missing_key(reg):
    assert await reg.try_begin_save("nope") is False


async def test_finish_save_spares_entry_reregistered_mid_save(reg):
    """M24: reaper claims a cold session, then a user turn re-registers the
    channel with a NEW sid before the multi-minute save finishes.
    finish_save(old_sid) must NOT delete the new registration."""
    await reg.register("telegram-123", "assistant", "old-sid")
    assert await reg.try_begin_save("telegram-123") is True
    # user turn completes mid-save: register() overwrites the claimed entry
    await reg.register("telegram-123", "assistant", "new-sid")
    # reaper's save completes for the OLD sid
    await reg.finish_save("telegram-123", "old-sid")
    entry = reg.get("telegram-123")
    assert entry is not None, "finish_save deleted the new session's registration"
    assert entry["sdk_session_id"] == "new-sid"


async def test_finish_save_pops_when_sid_matches(reg):
    await reg.register("telegram-123", "assistant", "old-sid")
    await reg.try_begin_save("telegram-123")
    await reg.finish_save("telegram-123", "old-sid")
    assert reg.get("telegram-123") is None


async def test_finish_save_none_sid_pops_unconditionally(reg):
    """Back-compat: passing no sid preserves the old unconditional pop."""
    await reg.register("telegram-123", "assistant", "old-sid")
    await reg.try_begin_save("telegram-123")
    await reg.register("telegram-123", "assistant", "new-sid")
    await reg.finish_save("telegram-123")
    assert reg.get("telegram-123") is None


async def test_clear_save_claim_spares_reregistered_entry(reg):
    """M24: clear_save_claim(old_sid) must not touch a newer registration."""
    await reg.register("telegram-123", "assistant", "old-sid")
    await reg.try_begin_save("telegram-123")
    await reg.register("telegram-123", "assistant", "new-sid")
    await reg.clear_save_claim("telegram-123", "old-sid")
    assert reg.get("telegram-123")["sdk_session_id"] == "new-sid"
