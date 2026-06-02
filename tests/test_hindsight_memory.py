# tests/test_hindsight_memory.py
"""HindsightSemanticMemory HTTP client. _request is patched so retain/recall/
render logic is tested without live HTTP (verified shapes in spec §8 findings)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hindsight_memory import HindsightSemanticMemory
from semantic_memory import SemanticMemory

pytestmark = [pytest.mark.unit]


def test_is_semantic_memory() -> None:
    assert issubclass(HindsightSemanticMemory, SemanticMemory)


async def test_retain_posts_verified_shape() -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"success": True, "items_count": 1})
    items = [{"content": "Nicola keeps the thermostat at 20C.",
              "tags": ["house"], "metadata": {"speaker": "nicola"},
              "document_id": "voice-1:0"}]
    await mem.retain("casa-assistant", items, async_=True)
    mem._request.assert_awaited_once()
    method, path, payload = mem._request.await_args.args
    assert method == "POST"
    assert path == "/v1/default/banks/casa-assistant/memories"
    assert payload["async"] is True          # top-level, not per-item (spec §8)
    assert payload["items"] == items


async def test_retain_validates_bank_id() -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock()
    with pytest.raises(ValueError):
        await mem.retain("casa/finance", [{"content": "x"}])  # bad bank id
    mem._request.assert_not_awaited()


async def test_recall_posts_verified_shape_and_renders() -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"results": [
        {"text": "Nicola keeps the thermostat at 20C.", "type": "world", "tags": ["house"]},
    ]})
    out = await mem.recall("casa-assistant", "thermostat?", tags=["house"], max_tokens=512, budget="low")
    method, path, payload = mem._request.await_args.args
    assert method == "POST"
    assert path == "/v1/default/banks/casa-assistant/memories/recall"
    assert payload["query"] == "thermostat?"
    assert payload["tags"] == ["house"]
    assert payload["tags_match"] == "any"
    assert payload["max_tokens"] == 512
    assert payload["budget"] == "low"
    assert "world" in payload["types"]        # spec §8.9 — must not drop world
    assert "thermostat at 20C" in out         # rendered digest


async def test_profile_gets_mental_models() -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"mental_models": [
        {"content": "Nicola: terse, prefers metric units."},
    ]})
    out = await mem.profile("casa-assistant")
    method, path, payload = mem._request.await_args.args
    assert method == "GET"
    assert path == "/v1/default/banks/casa-assistant/mental-models"
    assert payload is None
    assert "terse" in out


async def test_cross_recall_targets_other_bank() -> None:
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    mem._request = AsyncMock(return_value={"results": [{"text": "Budget is tight.", "type": "world"}]})
    out = await mem.cross_recall("casa-finance", "budget?", max_tokens=256)
    _, path, payload = mem._request.await_args.args
    assert path == "/v1/default/banks/casa-finance/memories/recall"
    assert payload["tags"] == []          # cross_recall = no tag filter (its defining semantic)
    assert payload["budget"] == "low"     # cross reads default cheap
    assert "Budget is tight" in out
