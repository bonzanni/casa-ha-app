"""Tests for cancel_engagement (Ellen-callable)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


class TestCancelEngagement:
    async def test_cancels_known_engagement(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import cancel_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )
        tch = MagicMock(); tch.send_to_topic = AsyncMock(); tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        res = await cancel_engagement.handler({"engagement_id": rec.id})
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "ok"
        assert rec.status == "cancelled"

    async def test_unknown_engagement_returns_error(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import cancel_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        res = await cancel_engagement.handler({"engagement_id": "nope"})
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "unknown_engagement"


async def test_cancel_writes_meta_scope_summary(tmp_path, monkeypatch):
    """M2.G4 (rewritten for the shared-bank rearch) — cancel must not be
    silent: it must retain a structured engagement summary on the shared
    `casa` bank with status=='cancelled'. Pre-fix passed memory_provider=None
    so cancellations were silent; the regression intent is preserved on the
    new delegated-memory mechanism."""
    import agent as agent_mod
    import delegated_memory
    from engagement_registry import EngagementRegistry
    from tools import cancel_engagement, init_tools

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "e.json"), bus=None,
    )
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t",
        origin={
            "role": "assistant", "channel": "telegram",
            "chat_id": "123", "cid": "abc",
        },
        topic_id=42,
    )

    # Recording semantic-memory fake exposed on the agent module the way the
    # production singleton would be.
    class _Sem:
        def __init__(self):
            self.retain_calls = []

        async def retain(self, bank, items, *, async_=True):
            self.retain_calls.append({"bank": bank, "items": items})

    sem = _Sem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)

    async def _fake_classify(text):
        return "private"

    monkeypatch.setattr(delegated_memory, "classify_tier", _fake_classify)

    tch = MagicMock()
    tch.send_to_topic = AsyncMock()
    tch.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = tch
    bus = MagicMock()
    bus.notify = AsyncMock()
    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    res = await cancel_engagement.handler({"engagement_id": rec.id})
    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "ok"

    # A structured engagement summary was retained on the shared `casa` bank
    # with status=='cancelled' — cancellation is not silent.
    assert sem.retain_calls, "expected a retain on cancel; got none"
    summaries = [
        json.loads(i["content"])
        for c in sem.retain_calls for i in c["items"]
    ]
    eng_summary = next(
        s for s in summaries if s["kind"] == "engagement_summary"
    )
    assert eng_summary["status"] == "cancelled"
    assert eng_summary["engagement_id"] == rec.id
