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
