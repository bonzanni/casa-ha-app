"""Task 5+6: _finalize_engagement retains the engagement summary on the
shared `casa` bank (tier-classified, write-trust gated) instead of writing to
the legacy Honcho meta/executor sessions. The post-back NOTIFICATION is
unchanged."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import delegated_memory

pytestmark = [pytest.mark.unit]


class _Sem:
    """Recording fake mirroring tests/test_delegated_memory.py's _Sem shape."""

    def __init__(self):
        self.recall_calls = []
        self.retain_calls = []

    async def recall(self, bank, query, *, tags, max_tokens, budget="mid", **kw):
        self.recall_calls.append({"bank": bank, "query": query})
        return ""

    async def retain(self, bank, items, *, async_=True):
        self.retain_calls.append({"bank": bank, "items": items})


def _install_sem(monkeypatch):
    """Inject a recording semantic-memory fake + a deterministic tier stub."""
    import agent as agent_mod

    sem = _Sem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)

    async def _fake_classify(text):
        return "private"

    monkeypatch.setattr(delegated_memory, "classify_tier", _fake_classify)
    return sem


async def _make_engagement(reg, *, channel, kind="specialist"):
    return await reg.create(
        kind=kind, role_or_type="finance", driver="in_casa", task="pay rent",
        origin={"role": "assistant", "channel": channel, "chat_id": "12345"},
        topic_id=42,
    )


async def test_telegram_summary_retained_and_postback_fires(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    sem = _install_sem(monkeypatch)

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await _make_engagement(reg, channel="telegram")

    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()

    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    driver = MagicMock()
    driver.cancel = AsyncMock()

    await _finalize_engagement(
        rec, outcome="completed", text="summary", artifacts=["sha1"],
        next_steps=[], driver=driver,
    )

    # One retain to the shared `casa` bank with the engagement_summary item.
    assert len(sem.retain_calls) == 1
    call = sem.retain_calls[0]
    assert call["bank"] == "casa"
    assert len(call["items"]) == 1
    item = call["items"][0]
    payload = json.loads(item["content"])
    assert payload["kind"] == "engagement_summary"
    assert payload["engagement_id"] == rec.id
    assert payload["task"] == "pay rent"
    assert payload["status"] == "completed"
    assert item["tags"] == ["private"]
    assert item["document_id"] == f"engagement:{rec.id}:summary:0"

    # Post-back NOTIFICATION still fired.
    bus.notify.assert_awaited_once()


async def test_voice_no_retain_but_postback_still_fires(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    sem = _install_sem(monkeypatch)

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await _make_engagement(reg, channel="voice")

    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()

    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    driver = MagicMock()
    driver.cancel = AsyncMock()

    await _finalize_engagement(
        rec, outcome="completed", text="summary", artifacts=[],
        next_steps=[], driver=driver,
    )

    # Voice is recall-only — write-trust gate blocks the retain.
    assert sem.retain_calls == []
    # But the post-back NOTIFICATION still fired.
    bus.notify.assert_awaited_once()


async def test_executor_kind_retains_distinct_doc_prefix(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    sem = _install_sem(monkeypatch)

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await _make_engagement(reg, channel="telegram", kind="executor")

    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic = AsyncMock()
    telegram.update_topic_state = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()

    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    driver = MagicMock()
    driver.cancel = AsyncMock()

    await _finalize_engagement(
        rec, outcome="completed", text="summary", artifacts=[],
        next_steps=[], driver=driver,
    )

    # Two retains: the engagement summary + the per-executor-type summary,
    # under DISTINCT document_ids so they do not clobber each other.
    assert len(sem.retain_calls) == 2
    doc_ids = [c["items"][0]["document_id"] for c in sem.retain_calls]
    assert any(d.endswith(":summary:0") for d in doc_ids)
    assert any(d.endswith(":executor_summary:0") for d in doc_ids)

    exec_call = next(
        c for c in sem.retain_calls
        if c["items"][0]["document_id"].endswith(":executor_summary:0")
    )
    exec_payload = json.loads(exec_call["items"][0]["content"])
    assert exec_payload["kind"] == "executor_engagement_summary"
    assert exec_payload["engagement_id"] == rec.id
