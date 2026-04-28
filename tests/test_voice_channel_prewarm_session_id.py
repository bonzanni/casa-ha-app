"""M2.G1 — voice prewarm must use the 4-segment session id shape so the
real-turn cache hit fires. Pre-fix it built `voice-{scope_id}-{role}`
which is the pre-3.2 shape; the agent uses
`{channel}-{chat_id}-{scope}-{role}` so the prewarm key never read."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.asyncio


def _make_voice_channel(memory, agent_configs):
    from channels.voice.channel import VoiceChannel

    # Construct the channel without booting the aiohttp server. The only
    # surfaces _prewarm touches are: self._memory, self._agent_configs,
    # self.default_agent. Bypass __init__ by direct attribute set.
    ch = VoiceChannel.__new__(VoiceChannel)
    ch._memory = memory
    ch._agent_configs = agent_configs
    ch.default_agent = "assistant"
    return ch


async def test_prewarm_warms_one_session_per_readable_scope():
    memory = SimpleNamespace(
        ensure_session=AsyncMock(return_value=None),
        get_context=AsyncMock(return_value=""),
    )
    cfg = SimpleNamespace(
        memory=SimpleNamespace(
            token_budget=1200,
            scopes_readable=["domestic", "finance", "meta"],
        ),
    )
    ch = _make_voice_channel(memory, {"assistant": cfg})

    await ch._prewarm("user-xyz")

    # One ensure_session + one get_context per readable scope.
    ensure_calls = memory.ensure_session.await_args_list
    get_calls = memory.get_context.await_args_list
    assert len(ensure_calls) == 3
    assert len(get_calls) == 3

    # 4-segment shape: voice-user-xyz-{scope}-assistant
    expected_sids = {
        "voice-user-xyz-domestic-assistant",
        "voice-user-xyz-finance-assistant",
        "voice-user-xyz-meta-assistant",
    }
    actual_sids = {c.kwargs["session_id"] for c in ensure_calls}
    assert actual_sids == expected_sids
    assert {c.kwargs["session_id"] for c in get_calls} == expected_sids

    # Per-scope token budget = 1200 // 3 = 400
    for c in get_calls:
        assert c.kwargs["tokens"] == 400


async def test_prewarm_continues_past_one_scope_failure(caplog):
    """A failure on one scope must not abort the rest — voice latency
    benefit on the surviving scopes still matters."""
    import logging

    async def ensure_side_effect(session_id, **_):
        if "finance" in session_id:
            raise RuntimeError("honcho 503")
        return None

    memory = SimpleNamespace(
        ensure_session=AsyncMock(side_effect=ensure_side_effect),
        get_context=AsyncMock(return_value=""),
    )
    cfg = SimpleNamespace(
        memory=SimpleNamespace(
            token_budget=900,
            scopes_readable=["domestic", "finance", "meta"],
        ),
    )
    ch = _make_voice_channel(memory, {"assistant": cfg})

    with caplog.at_level(logging.WARNING):
        await ch._prewarm("user-xyz")

    # finance ensure raised → its get_context did NOT run.
    # domestic + meta both completed.
    assert memory.get_context.await_count == 2
    assert any(
        "finance" in r.message for r in caplog.records
        if r.levelno == logging.WARNING
    )


async def test_prewarm_no_op_when_agent_config_missing():
    memory = SimpleNamespace(
        ensure_session=AsyncMock(),
        get_context=AsyncMock(),
    )
    ch = _make_voice_channel(memory, {})  # no assistant config
    await ch._prewarm("user-xyz")
    memory.ensure_session.assert_not_called()
    memory.get_context.assert_not_called()
