# tests/test_delegated_memory.py
"""Delegated memory bridges an originating context to the shared casa bank."""
import pytest

import delegated_memory
from personality_types import RetainedTurn, SpeakerProvenance

pytestmark = [pytest.mark.unit]


def _user(peer: str = "nicola") -> SpeakerProvenance:
    return SpeakerProvenance(speaker_kind="user", user_peer=peer)


def _resident(slot: str = "finance") -> SpeakerProvenance:
    return SpeakerProvenance(
        speaker_kind="resident", role_id=f"resident:{slot}", persona_id=f"casa/{slot}",
        persona_version="0.1.0", display_name=slot.capitalize(),
        binding_digest="sha256:" + "a" * 64,
    )


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
    assert sem.recall_calls[0]["budget"] == "low"   # explicit override still wins


async def test_delegated_recall_defaults_to_mid_budget():
    # The D-3 low-budget default (v0.68.1) was reverted in v0.69.4 once the
    # hindsight-side rerank latency was fixed: mid → 300 candidates gives
    # materially better recall quality and no longer risks the 20s client
    # timeout. Explicit budget= (e.g. voice) still overrides.
    sem = _Sem()
    await delegated_memory.delegated_recall(
        sem, query="q", origin_channel="telegram", max_tokens=2000,
    )
    assert sem.recall_calls[0]["budget"] == "mid"


async def test_delegated_recall_propagates_unavailable():
    """Three-outcome contract: backend unavailability is NOT collapsed to ''
    (which callers could not tell from a genuine zero-hit recall)."""
    from semantic_memory import RecallUnavailable

    class _Down:
        async def recall(self, *a, **k): raise RecallUnavailable("http_504")
    with pytest.raises(RecallUnavailable):
        await delegated_memory.delegated_recall(
            _Down(), query="q", origin_channel="telegram", max_tokens=10,
        )


async def test_delegated_recall_wraps_unexpected_errors_as_unavailable():
    """Any unexpected backend error still surfaces as UNAVAILABLE (typed),
    never as a raw exception and never as a fake zero-hit ''."""
    from semantic_memory import RecallUnavailable

    class _Boom:
        async def recall(self, *a, **k): raise RuntimeError("x")
    with pytest.raises(RecallUnavailable):
        await delegated_memory.delegated_recall(
            _Boom(), query="q", origin_channel="telegram", max_tokens=10,
        )


async def test_delegated_recall_empty_query_is_zero_hits_no_call():
    sem = _Sem()
    out = await delegated_memory.delegated_recall(
        sem, query="   ", origin_channel="telegram", max_tokens=10,
    )
    assert out == ""
    assert sem.recall_calls == []


async def test_retain_delegated_classifies_each_item(monkeypatch):
    async def fake_classify(text): return "private" if "salary" in text else "friends"
    monkeypatch.setattr(delegated_memory, "classify_tier", fake_classify)
    sem = _Sem()
    await delegated_memory.retain_delegated(
        sem, origin_channel="telegram",
        turns=[
            RetainedTurn("what is my salary", _user()),
            RetainedTurn("your salary is 5000", _resident()),
        ],
    )
    items = sem.retain_calls[0]["items"]
    assert sem.retain_calls[0]["bank"] == "casa"
    # Task 10: each item carries exactly one tier tag + one reserved provenance tag.
    assert [i["tags"][0] for i in items] == ["private", "private"]
    assert all(
        sum(1 for t in i["tags"] if t.startswith("casa-source-")) == 1 for i in items
    )
    # Content-addressed ids: user turn keyed on user_peer, agent turn on persona
    # identity — distinct, and each namespaced by kind.
    doc_ids = [i["document_id"] for i in items]
    assert doc_ids[0].startswith("m-") and not doc_ids[0].startswith("m-a-")
    assert doc_ids[1].startswith("m-a-")
    assert len(set(doc_ids)) == 2
    # Provenance survives into metadata for reconstruction.
    assert "casa_source_v1" in items[0]["metadata"]


async def test_retain_delegated_voice_writes_nothing():
    sem = _Sem()
    await delegated_memory.retain_delegated(
        sem, origin_channel="voice",
        turns=[RetainedTurn("anything", _resident("house"))],
    )
    assert sem.retain_calls == []   # voice = recall-only (write-trust)


async def test_retain_delegated_skips_blank_turns(monkeypatch):
    async def fake_classify(text): return "friends"
    monkeypatch.setattr(delegated_memory, "classify_tier", fake_classify)
    sem = _Sem()
    await delegated_memory.retain_delegated(
        sem, origin_channel="telegram",
        turns=[RetainedTurn("   ", _user()), RetainedTurn("real", _resident())],
    )
    items = sem.retain_calls[0]["items"]
    assert [i["content"] for i in items] == ["real"]   # blank turn dropped


async def test_run_delegated_agent_reads_the_callers_real_provenance_off_origin_var() -> None:
    """Regression: before this fix, origin_var never carried speaker_provenance at
    all, so a delegated turn's caller_provenance was always None — this proves
    _run_delegated_agent's parent.get("speaker_provenance") sees the real value
    Agent._process now sets."""
    import agent as agent_mod

    caller = SpeakerProvenance(
        speaker_kind="resident", role_id="resident:butler", persona_id="casa/tina",
        persona_version="0.1.0", display_name="Tina", binding_digest="sha256:" + "1" * 64,
    )
    token = agent_mod.origin_var.set({
        "role": "butler", "channel": "telegram", "execution_role": "butler",
        "speaker_provenance": caller,
    })
    try:
        import tools

        snapshot = tools._snapshot_origin()
        assert snapshot["speaker_provenance"] == caller
    finally:
        agent_mod.origin_var.reset(token)
