"""Unit coverage for _fetch_executor_archive — NEW semantic-recall behaviour.

The function now reads the shared ``casa`` bank via ``delegated_recall`` at the
originating engagement's read-clearance, keyed on the current task.  These
tests inject a fake ``active_semantic_memory`` directly onto the ``agent``
module so the lazy ``import agent as agent_mod`` inside the function picks it
up at call time.
"""
from __future__ import annotations

import pytest
import agent as agent_mod
from tools import _fetch_executor_archive

pytestmark = [pytest.mark.unit]


def _hit(text):
    from personality_types import RecallHit
    return RecallHit(
        text=text, memory_type="world", sensitivity="friends",
        application_tags=(), provenance=None, backend_id="b1", document_id=None,
        chunk_id=None, source_fact_ids=None, metadata=None, context=None, score=None,
    )


class _Sem:
    """Minimal fake that records recall calls and returns a canned digest."""

    def __init__(self, recall_ret: str = "prior lesson"):
        self.recall_calls: list[dict] = []
        self._recall_ret = recall_ret

    async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                           types=("world", "experience", "observation"),
                           tags_match="any", budget="mid"):
        self.recall_calls.append({
            "bank": bank,
            "query": query,
            "tags": sorted(tags),
            "max_tokens": max_tokens,
            "budget": budget,
            "clearance": clearance,
        })
        return (_hit(self._recall_ret),) if self._recall_ret else ()


# ---------------------------------------------------------------------------
# 1. Semantic recall at telegram clearance
# ---------------------------------------------------------------------------

async def test_semantic_recall_telegram_clearance(monkeypatch):
    """telegram origin → private clearance → all four tiers readable."""
    fake = _Sem("prior lesson")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake, raising=False)

    result = await _fetch_executor_archive(
        task="build the invoice",
        origin_channel="telegram",
        token_budget=3000,
    )

    assert len(fake.recall_calls) == 1
    c = fake.recall_calls[0]
    assert c["bank"] == "casa"
    assert c["query"] == "build the invoice"
    assert c["tags"] == ["family", "friends", "private", "public"]
    assert c["clearance"] == "private"
    assert c["max_tokens"] == 3000
    assert result.startswith("## Prior engagements (lessons learned)\n")
    assert "prior lesson" in result


# ---------------------------------------------------------------------------
# 2. Empty recall → empty string
# ---------------------------------------------------------------------------

async def test_empty_recall_returns_empty_string(monkeypatch):
    """When delegated_recall returns '' the function returns '' (no header)."""
    fake = _Sem("")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake, raising=False)

    result = await _fetch_executor_archive(
        task="do something",
        origin_channel="telegram",
        token_budget=2000,
    )

    assert result == ""


# ---------------------------------------------------------------------------
# 3. sem is None → empty string, no crash
# ---------------------------------------------------------------------------

async def test_sem_none_returns_empty_string(monkeypatch):
    """When active_semantic_memory is None the function returns '' gracefully."""
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None, raising=False)

    result = await _fetch_executor_archive(
        task="build the invoice",
        origin_channel="telegram",
        token_budget=3000,
    )

    assert result == ""


# ---------------------------------------------------------------------------
# 3b. Backend unavailable → empty string, no crash (delegated turn runs cold)
# ---------------------------------------------------------------------------

async def test_unavailable_recall_returns_empty_no_crash(monkeypatch):
    """RecallUnavailable from the seam must not crash the engagement spawn —
    the archive block is simply omitted (never a fabricated header)."""
    from semantic_memory import RecallUnavailable

    class _Down:
        async def recall_items(self, *a, **k): raise RecallUnavailable("timeout")

    monkeypatch.setattr(agent_mod, "active_semantic_memory", _Down(), raising=False)

    result = await _fetch_executor_archive(
        task="build the invoice",
        origin_channel="telegram",
        token_budget=3000,
    )

    assert result == ""


# ---------------------------------------------------------------------------
# 4. Voice clearance → reduced tag set
# ---------------------------------------------------------------------------

async def test_voice_clearance_uses_friends_tags(monkeypatch):
    """voice origin → friends clearance → only public + friends tiers."""
    fake = _Sem("- voice lesson")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake, raising=False)

    await _fetch_executor_archive(
        task="check temperature",
        origin_channel="voice",
        token_budget=1000,
    )

    assert len(fake.recall_calls) == 1
    assert fake.recall_calls[0]["tags"] == ["friends", "public"]


# ---------------------------------------------------------------------------
# #336 — the archive is filtered at the LAUNCHING TURN's clearance
# ---------------------------------------------------------------------------


async def test_non_operator_launch_gets_public_tags_only(monkeypatch):
    """Terra/Sol r3: the archive block is injected into the executor's prompt,
    so a non-operator-launched executor must not be handed private lessons.
    Red case: dropping origin_route/origin_clearance from the call restores
    channel-keyed private clearance and fails this test."""
    fake = _Sem("prior lesson")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake, raising=False)

    await _fetch_executor_archive(
        task="build the invoice",
        origin_channel="telegram",
        token_budget=3000,
        origin_route="telegram",
        origin_clearance="public",
    )

    c = fake.recall_calls[0]
    assert c["clearance"] == "public"
    assert c["tags"] == ["public"]


async def test_operator_launch_still_reads_every_tier(monkeypatch):
    fake = _Sem("prior lesson")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", fake, raising=False)

    await _fetch_executor_archive(
        task="build the invoice",
        origin_channel="telegram",
        token_budget=3000,
        origin_route="telegram",
        origin_clearance="private",
    )

    c = fake.recall_calls[0]
    assert c["clearance"] == "private"
    assert c["tags"] == ["family", "friends", "private", "public"]
