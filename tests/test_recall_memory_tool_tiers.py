"""recall_memory tool recalls the shared bank, filtered by channel clearance."""
import pytest

import agent as agent_mod
import tools

pytestmark = [pytest.mark.unit]


class _RecordingSem:
    def __init__(self):
        self.calls = []

    async def recall(self, bank, query, *, tags, max_tokens, budget="mid", **kw):
        self.calls.append({"bank": bank, "tags": sorted(tags), "budget": budget})
        return "- a fact"


async def test_voice_recall_uses_shared_bank_and_friends_clearance(monkeypatch):
    sem = _RecordingSem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    token = agent_mod.origin_var.set({"role": "butler", "channel": "voice"})
    try:
        out = await tools.recall_memory.handler({"query": "thermostat"})
    finally:
        agent_mod.origin_var.reset(token)
    assert sem.calls[0]["bank"] == "casa"
    assert sem.calls[0]["tags"] == ["friends", "public"]
    assert sem.calls[0]["budget"] == "low"
    assert out["content"][0]["text"]


async def test_telegram_recall_sees_all_tiers(monkeypatch):
    sem = _RecordingSem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram"})
    try:
        await tools.recall_memory.handler({"query": "salary"})
    finally:
        agent_mod.origin_var.reset(token)
    assert sem.calls[0]["bank"] == "casa"
    assert sem.calls[0]["tags"] == ["family", "friends", "private", "public"]
    assert sem.calls[0]["budget"] == "mid"
