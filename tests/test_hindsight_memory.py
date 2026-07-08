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



async def test_request_reuses_one_client_session(monkeypatch) -> None:
    """L32: _request must reuse one ClientSession across calls (keep-alive
    pooling), lazily replacing it only after close()."""
    created = []

    class FakeResp:
        def raise_for_status(self):
            pass

        async def json(self):
            return {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def __init__(self, *a, **kw):
            created.append(self)
            self.closed = False

        def request(self, *a, **kw):
            return FakeResp()

        async def close(self):
            self.closed = True

    monkeypatch.setattr("hindsight_memory.aiohttp.ClientSession", FakeSession)
    mem = HindsightSemanticMemory(base_url="http://hs:8888")
    await mem._request("GET", "/v1/default/banks/casa-assistant/mental-models")
    await mem._request(
        "POST", "/v1/default/banks/casa-assistant/memories/recall", {"query": "x"},
    )
    assert len(created) == 1, "ClientSession must be created once and reused"
    await mem.close()
    assert created[0].closed, "close() must close the shared session"
    await mem._request("GET", "/v1/default/banks/casa-assistant/mental-models")
    assert len(created) == 2, "a closed session must be lazily replaced, not reused"
    await mem.close()
