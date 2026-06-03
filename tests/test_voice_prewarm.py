"""Spec §4.3 — voice prewarmer warms the SemanticMemory profile overlay.

The prewarmer should issue a single cheap profile() GET before the user
speaks, targeting the bank_id("casa", default_agent) overlay.  No per-scope
fan-out; the legacy ensure_session / get_context path is gone.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.unit]


def _make_voice_channel(memory, agent_configs, default_agent="butler"):
    from channels.voice.channel import VoiceChannel

    # Bypass __init__ — _prewarm only touches _memory, _agent_configs,
    # and default_agent, so direct attribute assignment is sufficient.
    ch = VoiceChannel.__new__(VoiceChannel)
    ch._memory = memory
    ch._agent_configs = agent_configs
    ch.default_agent = default_agent
    return ch


async def test_prewarm_warms_profile_overlay():
    """A single profile() call is issued; no per-scope fan-out."""
    sem = AsyncMock()
    sem.profile.return_value = "- terse; metric units."

    cfg = SimpleNamespace()  # non-None cfg so the guard passes
    ch = _make_voice_channel(sem, {"butler": cfg})

    await ch._prewarm("room-1")

    sem.profile.assert_awaited_once()
    bank = sem.profile.await_args.args[0]
    assert "casa-butler" in bank


async def test_prewarm_no_op_when_agent_config_missing():
    """If the default_agent has no config, prewarm returns without calling profile."""
    sem = AsyncMock()
    ch = _make_voice_channel(sem, {})  # no "butler" entry

    await ch._prewarm("room-1")

    sem.profile.assert_not_awaited()


async def test_prewarm_swallows_profile_error(caplog):
    """A failing profile() is caught and logged as WARNING; no exception propagates."""
    sem = AsyncMock()
    sem.profile.side_effect = RuntimeError("hindsight 503")

    cfg = SimpleNamespace()
    ch = _make_voice_channel(sem, {"butler": cfg})

    with caplog.at_level(logging.WARNING):
        await ch._prewarm("room-1")  # must not raise

    assert any("prewarm" in r.message.lower() for r in caplog.records)
