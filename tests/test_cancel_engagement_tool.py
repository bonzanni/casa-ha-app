"""Tests for cancel_engagement (Ellen-callable)."""

from __future__ import annotations

import asyncio
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

    # L33 moved the retains off the turn's critical path into background
    # tasks (_finalize_engagement schedules retain_delegated via
    # asyncio.create_task) — drain them before asserting.
    import tools as tools_mod
    pending = list(tools_mod._specialist_bg_tasks)
    if pending:
        await asyncio.gather(*pending)

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


class TestCancelFinalizeContract:
    """#289: cancel_engagement must not report ok when the terminal persist
    failed and the record is still live — same contract as emit_completion's
    finalize_persist_failed. Red case demonstrated: reverting the tool to
    ignore _finalize_engagement's FinalizeResult fails both tests."""

    async def _setup(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import init_tools

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None,
        )
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )
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
        return reg, rec

    async def test_persist_failure_surfaces_retryable_error(
        self, tmp_path, monkeypatch,
    ):
        from tools import cancel_engagement

        reg, rec = await self._setup(tmp_path)
        # Strict persistence: a tombstone-write failure rolls back and
        # re-raises inside try_transition_terminal.
        monkeypatch.setattr(
            reg, "try_transition_terminal",
            AsyncMock(side_effect=OSError("disk full")),
        )
        res = await cancel_engagement.handler({"engagement_id": rec.id})
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "finalize_persist_failed"
        assert payload["retryable"] is True
        # The record really is still live — the caller must be told so.
        assert rec.status not in ("completed", "cancelled", "error")

    async def test_lost_terminal_race_reports_already_terminal(
        self, tmp_path, monkeypatch,
    ):
        from tools import cancel_engagement

        reg, rec = await self._setup(tmp_path)
        # A concurrent finalizer won between the tool's pre-check and the
        # registry's critical section.
        monkeypatch.setattr(
            reg, "try_transition_terminal", AsyncMock(return_value=False),
        )
        res = await cancel_engagement.handler({"engagement_id": rec.id})
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "acknowledged"
        assert payload["kind"] == "already_terminal"
