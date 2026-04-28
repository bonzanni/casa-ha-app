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
        tch = MagicMock(); tch.send_to_topic = AsyncMock(); tch.close_topic_with_check = AsyncMock()
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
    """M2.G4 — cancel must write the engagement summary into the
    engager's meta scope on Honcho, mirroring emit_completion's path.
    Pre-fix passed memory_provider=None so cancellations were silent."""
    import sys
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

    # Mock memory provider; expose it on the agent module the way the
    # production singleton would be.
    mp = MagicMock()
    mp.ensure_session = AsyncMock(return_value=None)
    mp.add_turn = AsyncMock(return_value=None)

    fake_agent_mod = MagicMock()
    fake_agent_mod.active_memory_provider = mp
    fake_agent_mod.active_engagement_driver = None
    fake_agent_mod.active_claude_code_driver = None
    monkeypatch.setitem(sys.modules, "agent", fake_agent_mod)

    tch = MagicMock()
    tch.send_to_topic = AsyncMock()
    tch.close_topic_with_check = AsyncMock()
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

    # Meta-scope summary write fired exactly once.
    meta_sid = "telegram-123-meta-assistant"
    assert any(
        c.kwargs.get("session_id") == meta_sid
        for c in mp.ensure_session.await_args_list
    ), f"expected ensure_session({meta_sid!r}); got {mp.ensure_session.await_args_list}"
    assert any(
        c.kwargs.get("session_id") == meta_sid
        for c in mp.add_turn.await_args_list
    )

    # Per-executor-type archival also fired (kind=executor branch).
    type_sid = "telegram-123-executor-configurator"
    assert any(
        c.kwargs.get("session_id") == type_sid
        for c in mp.add_turn.await_args_list
    )
