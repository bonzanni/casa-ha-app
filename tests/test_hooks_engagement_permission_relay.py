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


class TestEngagementResolution:
    async def test_engagement_not_in_registry(self):
        from hooks import make_engagement_permission_relay
        hook = make_engagement_permission_relay(
            engagement_registry=_FakeRegistry(),
            telegram_channel=_FakeTelegramChannel(),
            queues={},
        )
        cwd = "/data/engagements/" + "a" * 32
        result = await hook(
            {"tool_name": "Read", "tool_input": {}, "cwd": cwd},
            None, {},
        )
        assert _decision(result) == "deny"
        assert "unknown or inactive" in _reason(result)

    async def test_inactive_engagement(self):
        from hooks import make_engagement_permission_relay
        eid = "b" * 32
        reg = _FakeRegistry({eid: _FakeRecord(status="completed")})
        hook = make_engagement_permission_relay(
            engagement_registry=reg,
            telegram_channel=_FakeTelegramChannel(),
            queues={},
        )
        result = await hook(
            {"tool_name": "Read", "tool_input": {},
             "cwd": f"/data/engagements/{eid}"},
            None, {},
        )
        assert _decision(result) == "deny"
        assert "unknown or inactive" in _reason(result)

    async def test_cwd_subdir_resolves_and_allow_listed(self):
        from hooks import make_engagement_permission_relay
        eid = "c" * 32
        reg = _FakeRegistry({eid: _FakeRecord(tools_allowed=("Read",))})
        hook = make_engagement_permission_relay(
            engagement_registry=reg,
            telegram_channel=_FakeTelegramChannel(),
            queues={},
        )
        # cwd is a sub-directory of the engagement workspace — should still resolve.
        result = await hook(
            {"tool_name": "Read", "tool_input": {},
             "cwd": f"/data/engagements/{eid}/src"},
            None, {},
        )
        # tools_allowed=("Read",) so it should pass-through
        assert result == {}


class TestVerdictRelay:
    async def test_happy_path_allow(self):
        from hooks import make_engagement_permission_relay
        eid = "d" * 32
        reg = _FakeRegistry({eid: _FakeRecord(tools_allowed=())})
        tg = _FakeTelegramChannel()
        q = asyncio.Queue()
        await q.put({"request_id": "tuse_12345", "verdict": "allow"})
        hook = make_engagement_permission_relay(
            engagement_registry=reg,
            telegram_channel=tg,
            queues={eid: q},
            timeout_s=1.0,
        )
        result = await hook(
            {"tool_name": "Bash",
             "tool_input": {"command": "curl example.com"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_12345"},
            None, {},
        )
        assert result == {}
        # State transitioned: awaiting first, then active.
        assert tg.state_calls == [(eid, "awaiting"), (eid, "active")]
        # Keyboard was posted exactly once with the right request_id.
        assert len(tg.keyboard_calls) == 1
        kw = tg.keyboard_calls[0]
        assert kw["engagement_id"] == eid
        assert kw["request_id"] == "tuse_12345"
        assert kw["tool_name"] == "Bash"

    async def test_happy_path_deny(self):
        from hooks import make_engagement_permission_relay
        eid = "e" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()
        q = asyncio.Queue()
        await q.put({"request_id": "tuse_xyz", "verdict": "deny"})
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg,
            queues={eid: q}, timeout_s=1.0,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_xyz"},
            None, {},
        )
        assert _decision(result) == "deny"
        assert "operator denied" in _reason(result)
        assert tg.state_calls == [(eid, "awaiting"), (eid, "active")]

    async def test_timeout(self):
        from hooks import make_engagement_permission_relay
        eid = "f" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()
        q = asyncio.Queue()  # no verdict pushed
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg,
            queues={eid: q}, timeout_s=0.1,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "curl x"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_T"},
            None, {},
        )
        assert _decision(result) == "deny"
        assert "operator timeout" in _reason(result)
        # State returned to active even on timeout.
        assert tg.state_calls[-1] == (eid, "active")

    async def test_stale_verdict_drained(self):
        from hooks import make_engagement_permission_relay
        eid = "1" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()
        q = asyncio.Queue()
        # Stale verdict from previous timed-out request.
        await q.put({"request_id": "stale_rid", "verdict": "allow"})
        # Real verdict for current request.
        await q.put({"request_id": "current_rid", "verdict": "allow"})
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg,
            queues={eid: q}, timeout_s=1.0,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "x"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "current_rid"},
            None, {},
        )
        assert result == {}, "current verdict honoured, stale dropped"


class TestKeyboardFailure:
    async def test_keyboard_post_raises(self):
        from hooks import make_engagement_permission_relay
        eid = "2" * 32

        class _BrokenTg:
            def __init__(self):
                self.state_calls = []
            async def update_topic_state(self, *, engagement_id, new_state):
                self.state_calls.append((engagement_id, new_state))
            async def post_perm_keyboard(self, **kw):
                raise RuntimeError("network down")

        tg = _BrokenTg()
        hook = make_engagement_permission_relay(
            engagement_registry=_FakeRegistry({eid: _FakeRecord()}),
            telegram_channel=tg, queues={eid: asyncio.Queue()},
            timeout_s=1.0,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "x"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "rid"},
            None, {},
        )
        assert _decision(result) == "deny"
        assert "keyboard post failed" in _reason(result)
        assert "network down" in _reason(result)
        # State returned to active even on keyboard failure.
        assert tg.state_calls[-1] == (eid, "active")
