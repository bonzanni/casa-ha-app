"""Tests for the claude_code driver stub (Plan 5 will fill in)."""

import pytest

pytestmark = pytest.mark.asyncio


class TestClaudeCodeStub:
    async def test_start_raises_not_implemented(self):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from engagement_registry import EngagementRecord

        drv = ClaudeCodeDriver()
        rec = EngagementRecord(
            id="e1", kind="executor", role_or_type="plugin-developer",
            driver="claude_code", status="active", topic_id=None,
            started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None, origin={}, task="x",
        )
        with pytest.raises(NotImplementedError, match="Plan 5"):
            await drv.start(rec, prompt="p", options=None)

    async def test_is_alive_false(self):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from engagement_registry import EngagementRecord

        drv = ClaudeCodeDriver()
        rec = EngagementRecord(
            id="e1", kind="executor", role_or_type="plugin-developer",
            driver="claude_code", status="active", topic_id=None,
            started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None, origin={}, task="x",
        )
        assert drv.is_alive(rec) is False
