"""L33: _finalize_engagement must not block on the retain tier-classification.

The two retain_delegated calls (engagement summary + executor summary) each run
an LLM tier-classification subprocess; L33 moved them OFF the finalize critical
path into background tasks. These tests pin:
  1. finalize returns promptly even while classification is still gated, and the
     retain lands once the background task is drained; and
  2. the H-1 invariant survives — on the deferred-hard-reload path the Supervisor
     restart still fires only AFTER the retain writes have landed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import delegated_memory

pytestmark = [pytest.mark.unit]


class _Sem:
    def __init__(self):
        self.retain_calls = []

    async def recall(self, bank, query, *, tags, max_tokens, budget="mid", **kw):
        return ""

    async def retain(self, bank, items, *, async_=True):
        self.retain_calls.append({"bank": bank, "items": items})


async def _make_engagement(reg, *, channel="telegram", kind="specialist"):
    return await reg.create(
        kind=kind, role_or_type="finance", driver="in_casa", task="pay rent",
        origin={"role": "assistant", "channel": channel, "chat_id": "12345"},
        topic_id=42,
    )


def _wire(reg):
    from tools import init_tools
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


async def test_finalize_does_not_block_on_classification(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement
    import agent as agent_mod
    import tools as tools_mod

    sem = _Sem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)

    gate = asyncio.Event()

    async def _gated_classify(text):
        await gate.wait()
        return "private"

    monkeypatch.setattr(delegated_memory, "classify_tier", _gated_classify)

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await _make_engagement(reg)
    _wire(reg)

    driver = MagicMock()
    driver.cancel = AsyncMock()

    # Old inline code would hang here (classify never returns); the fix returns.
    await asyncio.wait_for(
        _finalize_engagement(
            rec, outcome="completed", text="summary", artifacts=[],
            next_steps=[], driver=driver,
        ),
        timeout=1.0,
    )
    # Retain is still pending (classification gated).
    assert sem.retain_calls == []

    # Release the gate and drain the background retain.
    gate.set()
    await asyncio.gather(*list(tools_mod._specialist_bg_tasks), return_exceptions=True)
    assert len(sem.retain_calls) == 1
    # Task 10: content-addressed agent id (m-a- space) instead of the retired
    # doc_prefix:idx scheme; the payload identifies the engagement summary.
    import json
    item = sem.retain_calls[0]["items"][0]
    assert item["document_id"].startswith("m-a-")
    assert json.loads(item["content"])["kind"] == "engagement_summary"


async def test_deferred_reload_waits_for_retain(tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    import tools as tools_mod
    from tools import _finalize_engagement
    import agent as agent_mod

    sem = _Sem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)

    events: list[str] = []

    async def _classify(text):
        await asyncio.sleep(0)
        events.append("classified")
        return "private"

    monkeypatch.setattr(delegated_memory, "classify_tier", _classify)

    async def _fake_restart():
        events.append("restart_posted")
        return {"status": "ok", "supervisor_status": 200}

    monkeypatch.setattr(tools_mod, "_post_supervisor_restart", _fake_restart)

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await _make_engagement(reg)
    _wire(reg)
    tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD.add(rec.id)

    driver = MagicMock()
    driver.cancel = AsyncMock()

    await _finalize_engagement(
        rec, outcome="completed", text="summary", artifacts=[],
        next_steps=[], driver=driver,
    )

    # H-1: the deferred Supervisor restart fires only AFTER the retain landed.
    assert events == ["classified", "restart_posted"]
    assert len(sem.retain_calls) == 1
