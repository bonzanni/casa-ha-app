"""AR-2: origin readers must snapshot at entry — a delegation spawned
during a voice turn must keep voice-channel origin (clearance gate) even
after the shared origin holder is rewritten by a later telegram turn."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_delegation_parent_origin_survives_holder_rewrite(monkeypatch):
    import agent as agent_mod
    import tools

    holder = {
        "cid": "voice-cid", "channel": "voice", "chat_id": "s1",
        "role": "butler", "user_text": "hi",
        "voice_transport": "ws", "voice_route_id": "entry-1",
        "voice_route_capabilities": frozenset({"background_jobs"}),
        "origin_device_id": "device-kitchen",
    }
    token = agent_mod.origin_var.set(holder)
    try:
        captured = {}

        # _run_delegated_agent reads parent origin AFTER the delegated turn;
        # intercept the seam that consumes it (retain gate inputs).
        parent = tools._snapshot_origin()          # new helper under test
        captured["before"] = dict(parent)
        # Simulate the next turn rewriting the SAME holder in place:
        holder.clear()
        holder.update({"cid": "tg-cid", "channel": "telegram"})
        captured["after"] = dict(parent)
    finally:
        agent_mod.origin_var.reset(token)

    assert captured["before"]["channel"] == "voice"
    assert captured["after"]["channel"] == "voice"     # snapshot, not reference
    assert captured["after"]["voice_route_id"] == "entry-1"
    assert captured["after"]["origin_device_id"] == "device-kitchen"
