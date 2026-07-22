"""Task 5+6: query_engager reads from the shared `casa` bank via
delegated_recall at the engagement origin's read-clearance, instead of the
legacy Honcho session get_context."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.unit]


def _hit(text):
    from personality_types import RecallHit
    return RecallHit(
        text=text, memory_type="world", sensitivity="friends",
        application_tags=(), provenance=None, backend_id="b1", document_id=None,
        chunk_id=None, source_fact_ids=None, metadata=None, context=None, score=None,
    )


class _Sem:
    """Recording fake mirroring tests/test_delegated_memory.py's _Sem shape."""

    def __init__(self, recall_ret="Lesina paid in March."):
        self.recall_calls = []
        self.retain_calls = []
        self._recall_ret = recall_ret

    async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                           types=("world", "experience", "observation"),
                           tags_match="any", budget="mid"):
        self.recall_calls.append({
            "bank": bank, "query": query, "tags": sorted(tags),
            "max_tokens": max_tokens, "budget": budget, "clearance": clearance,
        })
        return (_hit(self._recall_ret),) if self._recall_ret else ()

    async def retain(self, bank, items, *, async_=True):
        self.retain_calls.append({"bank": bank, "items": items})


async def _setup(reg, sem, monkeypatch, *, synth_answer="Yes, Lesina paid in March."):
    from tools import init_tools

    bus = MagicMock()

    async def _notify(*a, **k):
        return None

    bus.notify = _notify
    init_tools(
        channel_manager=MagicMock(), bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)

    async def _fake_synth(question, context, max_tokens):
        return synth_answer

    monkeypatch.setattr("tools._synthesize_answer", _fake_synth)


async def test_telegram_clearance_recall(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    from tools import query_engager, engagement_var

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram",
                          "chat_id": "c1"},
        topic_id=42,
    )
    sem = _Sem()
    await _setup(reg, sem, monkeypatch)

    token = engagement_var.set(rec)
    try:
        res = await query_engager.handler(
            {"question": "when did Lesina pay?", "max_tokens": 500},
        )
    finally:
        engagement_var.reset(token)

    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "ok"
    assert payload["text"] == "Yes, Lesina paid in March."

    assert len(sem.recall_calls) == 1
    c = sem.recall_calls[0]
    assert c["bank"] == "casa"
    assert c["query"] == "when did Lesina pay?"
    assert c["tags"] == ["family", "friends", "private", "public"]
    assert c["max_tokens"] == 2000


async def test_empty_recall_returns_unknown(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    from tools import query_engager, engagement_var

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram",
                          "chat_id": "c1"},
        topic_id=42,
    )
    sem = _Sem(recall_ret="")
    await _setup(reg, sem, monkeypatch)

    token = engagement_var.set(rec)
    try:
        res = await query_engager.handler({"question": "x", "max_tokens": 500})
    finally:
        engagement_var.reset(token)

    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "unknown"


async def test_unavailable_recall_returns_unavailable_not_unknown(tmp_path, monkeypatch):
    """Three-outcome contract: engager memory that could NOT be checked is
    'unavailable', distinct from a genuine zero-hit 'unknown'."""
    from engagement_registry import EngagementRegistry
    from semantic_memory import RecallUnavailable
    from tools import query_engager, engagement_var

    class _Down(_Sem):
        async def recall_items(self, *a, **k): raise RecallUnavailable("http_504")

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram",
                          "chat_id": "c1"},
        topic_id=42,
    )
    await _setup(reg, _Down(), monkeypatch)

    token = engagement_var.set(rec)
    try:
        res = await query_engager.handler({"question": "x", "max_tokens": 500})
    finally:
        engagement_var.reset(token)

    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "unavailable"
    assert "could not be checked" in payload["message"]
    # The tool description enumerates all three statuses.
    for status in ("status=ok", "status=unknown", "status=unavailable"):
        assert status in query_engager.description
