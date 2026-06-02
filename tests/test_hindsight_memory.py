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
