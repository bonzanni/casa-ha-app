"""Tests for engagement_permission_relay PreToolUse hook."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


def _reason(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


class _FakeRecord:
    def __init__(self, status="active", tools_allowed=()):
        self.status = status
        self.tools_allowed = tuple(tools_allowed)


class _FakeRegistry:
    def __init__(self, records: dict | None = None):
        self._records = records or {}

    def get(self, eid):
        return self._records.get(eid)


class _FakeTelegramChannel:
    def __init__(self):
        self.state_calls = []
        self.keyboard_calls = []

    async def update_topic_state(self, *, engagement_id, new_state):
        self.state_calls.append((engagement_id, new_state))

    async def post_perm_keyboard(self, **kw):
        self.keyboard_calls.append(kw)


class TestUnknownContext:
    async def test_cwd_not_under_engagements(self):
        from hooks import make_engagement_permission_relay
        hook = make_engagement_permission_relay(
            engagement_registry=_FakeRegistry(),
            telegram_channel=_FakeTelegramChannel(),
            queues={},
        )
        result = await hook(
            {"tool_name": "Read", "tool_input": {}, "cwd": "/etc"},
            None, {},
        )
        assert _decision(result) == "deny"
        assert "engagement context" in _reason(result)
