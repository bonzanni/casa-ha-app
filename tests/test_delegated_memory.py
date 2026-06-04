# tests/test_delegated_memory.py
"""Delegated memory bridges an originating context to the shared casa bank."""
import pytest

import delegated_memory

pytestmark = [pytest.mark.unit]


class _Sem:
    def __init__(self, recall_ret="- prior fact"):
        self.recall_calls = []
        self.retain_calls = []
        self._recall_ret = recall_ret

    async def recall(self, bank, query, *, tags, max_tokens, budget="mid", **kw):
        self.recall_calls.append({"bank": bank, "query": query, "tags": sorted(tags), "budget": budget})
        return self._recall_ret

    async def retain(self, bank, items, *, async_=True):
        self.retain_calls.append({"bank": bank, "items": items})


async def test_delegated_recall_uses_inherited_clearance():
    sem = _Sem()
    out = await delegated_memory.delegated_recall(
        sem, query="build the invoice", origin_channel="telegram", max_tokens=2000,
    )
    assert out == "- prior fact"
    c = sem.recall_calls[0]
    assert c["bank"] == "casa"
    assert c["tags"] == ["family", "friends", "private", "public"]   # telegram → private clearance


async def test_delegated_recall_voice_is_friends():
    sem = _Sem()
    await delegated_memory.delegated_recall(
        sem, query="q", origin_channel="voice", max_tokens=500, budget="low",
    )
    assert sem.recall_calls[0]["tags"] == ["friends", "public"]
    assert sem.recall_calls[0]["budget"] == "low"


async def test_delegated_recall_swallows_errors():
    class _Boom:
        async def recall(self, *a, **k): raise RuntimeError("x")
    out = await delegated_memory.delegated_recall(
        _Boom(), query="q", origin_channel="telegram", max_tokens=10,
    )
    assert out == ""   # leak-safe / never crashes the delegated turn


async def test_retain_delegated_classifies_each_item(monkeypatch):
    async def fake_classify(text): return "private" if "salary" in text else "friends"
    monkeypatch.setattr(delegated_memory, "classify_tier", fake_classify)
    sem = _Sem()
    await delegated_memory.retain_delegated(
        sem, origin_channel="telegram", doc_prefix="delegation:cid1:finance",
        turns=[("user", "what is my salary"), ("assistant", "your salary is 5000")],
    )
    items = sem.retain_calls[0]["items"]
    assert sem.retain_calls[0]["bank"] == "casa"
    assert [i["tags"] for i in items] == [["private"], ["private"]]
    assert [i["document_id"] for i in items] == ["delegation:cid1:finance:0", "delegation:cid1:finance:1"]


async def test_retain_delegated_voice_writes_nothing():
    sem = _Sem()
    await delegated_memory.retain_delegated(
        sem, origin_channel="voice", doc_prefix="delegation:cid2:house",
        turns=[("assistant", "anything")],
    )
    assert sem.retain_calls == []   # voice = recall-only (write-trust)


async def test_retain_delegated_skips_blank_turns(monkeypatch):
    async def fake_classify(text): return "friends"
    monkeypatch.setattr(delegated_memory, "classify_tier", fake_classify)
    sem = _Sem()
    await delegated_memory.retain_delegated(
        sem, origin_channel="telegram", doc_prefix="d:1",
        turns=[("user", "   "), ("assistant", "real")],
    )
    items = sem.retain_calls[0]["items"]
    assert [i["content"] for i in items] == ["real"]
    assert [i["document_id"] for i in items] == ["d:1:1"]   # index preserved from the original turn list
