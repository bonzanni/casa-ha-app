"""Tests for the shared _finalize_engagement helper in tools.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


class TestFinalizeEngagement:
    async def test_happy_path_closes_topic_and_notifies_ellen(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.close_topic_with_check = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()
        memory = MagicMock()
        memory.add_turn = AsyncMock()
        memory.ensure_session = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        driver = MagicMock()
        driver.cancel = AsyncMock()

        await _finalize_engagement(
            rec, outcome="completed", text="summary", artifacts=["sha1"],
            next_steps=[], driver=driver, memory_provider=memory,
        )

        # Topic closed + icon flipped
        telegram.close_topic_with_check.assert_awaited_once_with(thread_id=42)
        # Completion message posted in topic
        telegram.send_to_topic.assert_awaited()
        # NOTIFICATION sent to Ellen
        bus.notify.assert_awaited_once()
        # Driver cancelled
        driver.cancel.assert_awaited_once_with(rec)
        # Meta-scope summary written
        memory.add_turn.assert_awaited_once()
        # Record status is completed
        assert rec.status == "completed"
        assert rec.completed_at is not None

    async def test_cancel_outcome_uses_cancel_path(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.close_topic_with_check = AsyncMock()
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
            rec, outcome="cancelled", text="user cancelled",
            artifacts=[], next_steps=[], driver=driver, memory_provider=None,
        )
        assert rec.status == "cancelled"
        driver.cancel.assert_awaited_once_with(rec)


async def test_finalize_writes_retention_for_claude_code_driver(
    tmp_path, monkeypatch,
):
    """Plan 4a.1: _finalize_engagement must update .casa-meta.json with
    retention_until = now + 7 days when driver=='claude_code' and a
    workspace dir exists."""
    import json
    import tools as tools_mod
    from engagement_registry import EngagementRecord, EngagementRegistry
    from drivers.workspace import write_casa_meta
    from tools import _finalize_engagement

    ws = tmp_path / "eng1"
    ws.mkdir()
    write_casa_meta(
        workspace_path=str(ws), engagement_id="eng1",
        executor_type="hello-driver", status="UNDERGOING",
        created_at="2026-04-23T08:00:00Z",
        finished_at=None, retention_until=None,
    )

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "tomb.json"), bus=None)
    rec = EngagementRecord(
        id="eng1", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )
    reg._records["eng1"] = rec

    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_channel_manager", None)
    monkeypatch.setattr(tools_mod, "_bus", None)
    # Point the hardcoded /data/engagements path to tmp.
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    await _finalize_engagement(
        rec, outcome="completed", text="done",
        artifacts=[], next_steps=[], driver=None, memory_provider=None,
    )

    meta = json.loads((ws / ".casa-meta.json").read_text())
    assert meta["status"] == "COMPLETED"
    assert meta["retention_until"] is not None
    # Parseable as ISO 8601 Z.
    import re
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                        meta["retention_until"])
