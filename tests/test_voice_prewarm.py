"""Spec §4.3 — voice stt_start prewarm: session ensured, no overlay call.

Under the tiered-memory model voice clearance is 'friends', which is below the
'private' threshold required to push the overlay.  The overlay warm via
profile() was therefore dead work and has been removed.  stt_start now only
ensures a VoiceSession is bookmarked in the pool (for idle-sweep / dedup);
no memory I/O is performed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.unit]


def _make_voice_channel(memory, agent_configs, default_agent="butler"):
    from channels.voice.channel import VoiceChannel

    # Bypass __init__ — we only need pool + default_agent for the stt_start path.
    ch = VoiceChannel.__new__(VoiceChannel)
    ch._memory = memory
    ch._agent_configs = agent_configs
    ch.default_agent = default_agent

    from channels.voice.session import VoiceSessionPool
    ch.pool = VoiceSessionPool(idle_timeout=300)
    return ch


async def test_stt_start_ensures_session_no_profile():
    """stt_start no longer calls profile() — overlay is not used for voice."""
    sem = AsyncMock()
    cfg = SimpleNamespace()
    ch = _make_voice_channel(sem, {"butler": cfg})

    # Simulate what the WS handler does on stt_start.
    ch.pool.ensure("room-1")

    sem.profile.assert_not_awaited()
    assert ch.pool.get("room-1") is not None


async def test_no_prewarm_method_on_voice_channel():
    """VoiceChannel must not expose a _prewarm method after the cleanup."""
    from channels.voice.channel import VoiceChannel
    assert not hasattr(VoiceChannel, "_prewarm"), (
        "_prewarm was not removed; the obsolete overlay prewarm is still present"
    )


async def test_schedule_prewarm_not_called_on_stt_start():
    """schedule_prewarm should no longer be invoked on stt_start."""
    sem = AsyncMock()
    cfg = SimpleNamespace()
    ch = _make_voice_channel(sem, {"butler": cfg})

    pool_mock = MagicMock()
    pool_mock.ensure = MagicMock()
    pool_mock.schedule_prewarm = MagicMock()
    ch.pool = pool_mock

    # Simulate what the WS handler does on stt_start.
    ch.pool.ensure("room-1")

    pool_mock.ensure.assert_called_once_with("room-1")
    pool_mock.schedule_prewarm.assert_not_called()
