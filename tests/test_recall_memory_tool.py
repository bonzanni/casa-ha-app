"""recall_memory pull tool (spec §4.3): on-demand semantic recall against the
shared 'casa' bank, filtered by channel clearance tiers. Voice uses budget=low
so rerank never stalls a turn."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.unit]


def _text(res: dict) -> str:
    return res["content"][0]["text"]


def _setup(monkeypatch, *, channel: str):
    import agent as agent_mod
    import tools
    sem = AsyncMock()
    sem.recall.return_value = "- Nicola keeps the thermostat at 20C."
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    # caller config with token_budget
    cfg = SimpleNamespace(memory=SimpleNamespace(token_budget=512))
    monkeypatch.setattr(tools, "_agent_role_map", {"assistant": cfg}, raising=False)
    agent_mod.origin_var.set({"role": "assistant", "channel": channel})
    return sem


async def test_recall_memory_calls_semantic_recall_voice_low(monkeypatch):
    import tools
    sem = _setup(monkeypatch, channel="voice")
    res = await tools.recall_memory.handler({"query": "what temp do I like?"})
    sem.recall.assert_awaited_once()
    kw = sem.recall.await_args.kwargs
    assert sem.recall.await_args.args[0] == "casa"              # shared bank
    assert kw["tags"] == ["public", "friends"]                  # voice → friends clearance
    assert kw["budget"] == "low"                                # voice → low
    assert "thermostat at 20C" in _text(res)


async def test_recall_memory_text_budget_mid(monkeypatch):
    import tools
    sem = _setup(monkeypatch, channel="telegram")
    await tools.recall_memory.handler({"query": "temp?"})
    assert sem.recall.await_args.kwargs["budget"] == "mid"      # non-voice → mid


async def test_recall_memory_empty_query_errors(monkeypatch):
    import agent as agent_mod, tools
    monkeypatch.setattr(agent_mod, "active_semantic_memory", AsyncMock(), raising=False)
    res = await tools.recall_memory.handler({"query": "  "})
    assert "error" in _text(res).lower()


def test_agent_exposes_semantic_handle():
    import agent
    assert hasattr(agent, "active_semantic_memory")
