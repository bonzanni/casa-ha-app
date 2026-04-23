"""Tests for Honcho transcript archival in _finalize_engagement (§6.4)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


async def test_archival_writes_summary_to_executor_type_peer(monkeypatch):
    from tools import _finalize_engagement, init_tools
    from engagement_registry import EngagementRecord, EngagementRegistry

    memory_provider = MagicMock()
    memory_provider.ensure_session = AsyncMock()
    memory_provider.add_turn = AsyncMock()

    reg = EngagementRegistry(tombstone_path="/tmp/xx.json", bus=None)
    init_tools(None, None, None, engagement_registry=reg)

    rec = EngagementRecord(
        id="e-xyz", kind="executor", role_or_type="configurator",
        driver="in_casa", status="active", topic_id=None,
        started_at=100.0, last_user_turn_ts=150.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None,
        origin={"channel": "telegram", "chat_id": "42"},
        task="Install a skill onto Tina",
    )
    reg._records[rec.id] = rec

    await _finalize_engagement(
        rec, outcome="completed", text="Skill installed.",
        artifacts=["abc123"], next_steps=[],
        driver=None, memory_provider=memory_provider,
    )

    calls = memory_provider.add_turn.await_args_list
    session_ids = [c.kwargs["session_id"] for c in calls]
    # New: executor-type write also runs
    assert any(s == "telegram:42:executor:configurator" for s in session_ids)

    # The executor-type summary is a JSON blob with task + terminal_state + etc.
    exec_call = next(
        c for c in calls
        if c.kwargs["session_id"] == "telegram:42:executor:configurator"
    )
    summary = json.loads(exec_call.kwargs["assistant_text"])
    assert summary["terminal_state"] == "completed"
    assert summary["task"] == "Install a skill onto Tina"
    assert summary["executor_type"] == "configurator"
    assert summary["engagement_id"] == "e-xyz"
