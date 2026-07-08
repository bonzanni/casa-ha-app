"""Tests for engagement_permission_relay PreToolUse hook."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


def _reason(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


class _FakeRecord:
    def __init__(self, status="active", tools_allowed=(),
                 permission_mode="acceptEdits"):
        self.status = status
        self.tools_allowed = tuple(tools_allowed)
        self.permission_mode = permission_mode


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

    async def test_defaultdict_first_fire_does_not_instant_deny(self):
        """v0.37.3 regression: production passes a ``defaultdict(asyncio.Queue)``.
        The hook must wait on the auto-created queue for the timeout window,
        NOT instant-deny.

        Pre-v0.37.3 the hook used ``queues.get(eng_id)`` which returned None
        on first fire (defaultdict's factory only triggers on ``[]``), so
        every first non-allow-listed tool call was denied before the operator
        could tap. Verified live on N150 with engagement 4f0a3d6e.
        """
        import collections
        from hooks import make_engagement_permission_relay
        eid = "3" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()
        queues = collections.defaultdict(asyncio.Queue)
        # Nothing put on the queue — hook must wait the full timeout window.
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg,
            queues=queues, timeout_s=0.1,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "x"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "rid"},
            None, {},
        )
        # Must be operator timeout, not "no permission queue".
        assert _decision(result) == "deny"
        assert "operator timeout" in _reason(result), (
            f"expected timeout deny, got: {_reason(result)!r}"
        )
        # The queue must have been created via the [] auto-create.
        assert eid in queues
        # State returned to active even on timeout.
        assert tg.state_calls[-1] == (eid, "active")


class TestPermissionModeShortCircuit:
    """G-1 v0.37.7: executors with permission_mode={auto,bypassPermissions}
    bypass the relay hook so autonomous (no-operator-at-keyboard) runs work.
    acceptEdits and default still fall through to the allow-list + relay
    pipeline.
    """

    async def test_auto_short_circuits_without_keyboard(self):
        from hooks import make_engagement_permission_relay
        eid = "a" * 32
        reg = _FakeRegistry({
            eid: _FakeRecord(tools_allowed=(),  # empty -> would normally relay
                             permission_mode="auto"),
        })
        tg = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg,
            queues={}, timeout_s=0.01,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_auto"},
            None, {},
        )
        assert result == {}
        # No keyboard ever posted, no state transitions emitted.
        assert tg.keyboard_calls == []
        assert tg.state_calls == []

    async def test_bypass_permissions_short_circuits(self):
        from hooks import make_engagement_permission_relay
        eid = "b" * 32
        reg = _FakeRegistry({
            eid: _FakeRecord(tools_allowed=(),
                             permission_mode="bypassPermissions"),
        })
        tg = _FakeTelegramChannel()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg,
            queues={}, timeout_s=0.01,
        )
        result = await hook(
            {"tool_name": "Write",
             "tool_input": {"file_path": "/tmp/x", "content": "y"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_bp"},
            None, {},
        )
        assert result == {}
        assert tg.keyboard_calls == []
        assert tg.state_calls == []

    async def test_accept_edits_falls_through_to_relay(self):
        from hooks import make_engagement_permission_relay
        eid = "c" * 32
        reg = _FakeRegistry({
            eid: _FakeRecord(tools_allowed=(),  # nothing allow-listed
                             permission_mode="acceptEdits"),
        })
        tg = _FakeTelegramChannel()
        q = asyncio.Queue()  # no verdict — let it time out
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg,
            queues={eid: q}, timeout_s=0.05,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_ae"},
            None, {},
        )
        # acceptEdits MUST take the relay path: keyboard posted, then
        # operator-timeout deny lands.
        assert _decision(result) == "deny"
        assert "operator timeout" in _reason(result)
        assert len(tg.keyboard_calls) == 1
        assert tg.state_calls[0] == (eid, "awaiting")
        assert tg.state_calls[-1] == (eid, "active")

    async def test_default_mode_falls_through_to_relay(self):
        from hooks import make_engagement_permission_relay
        eid = "d" * 32
        reg = _FakeRegistry({
            eid: _FakeRecord(tools_allowed=(),
                             permission_mode="default"),
        })
        tg = _FakeTelegramChannel()
        q = asyncio.Queue()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg,
            queues={eid: q}, timeout_s=0.05,
        )
        result = await hook(
            {"tool_name": "Edit",
             "tool_input": {"file_path": "/x", "old_string": "a",
                            "new_string": "b"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_def"},
            None, {},
        )
        assert _decision(result) == "deny"
        assert "operator timeout" in _reason(result)
        assert len(tg.keyboard_calls) == 1

    async def test_missing_permission_mode_defaults_to_relay(self):
        """Defensive: records loaded from a pre-v0.37.7 tombstone may not
        carry permission_mode. The hook must default such records to the
        relay path (current behaviour), NOT silently bypass.
        """
        from hooks import make_engagement_permission_relay
        eid = "e" * 32

        class _OldRecord:
            status = "active"
            tools_allowed = ()
            # No permission_mode attribute at all.

        reg = _FakeRegistry({eid: _OldRecord()})
        tg = _FakeTelegramChannel()
        q = asyncio.Queue()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg,
            queues={eid: q}, timeout_s=0.05,
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls"},
             "cwd": f"/data/engagements/{eid}",
             "tool_use_id": "tuse_old"},
            None, {},
        )
        assert _decision(result) == "deny"
        assert "operator timeout" in _reason(result)


class TestConcurrentWaiters:
    """M18 (v0.53.0): two concurrent relays on one engagement must each
    receive THEIR OWN verdict. Pre-fix, both waiters shared one queue and
    each discarded the other's verdict, so both denied by timeout."""

    async def test_out_of_order_taps_resolve_both_requests(self):
        from hooks import make_engagement_permission_relay
        eid = "9" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()
        q = asyncio.Queue()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg,
            queues={eid: q}, timeout_s=2.0,
        )

        def payload(rid):
            return {"tool_name": "WebFetch", "tool_input": {"url": "https://x"},
                    "cwd": f"/data/engagements/{eid}", "tool_use_id": rid}

        t_a = asyncio.create_task(hook(payload("rid_A"), None, {}))
        t_b = asyncio.create_task(hook(payload("rid_B"), None, {}))
        await asyncio.sleep(0.05)  # both waiters registered

        # Operator taps the NEWEST keyboard (B) first, then A.
        await q.put({"request_id": "rid_B", "verdict": "allow"})
        await q.put({"request_id": "rid_A", "verdict": "allow"})

        res_a, res_b = await asyncio.wait_for(
            asyncio.gather(t_a, t_b), timeout=1.0,
        )
        assert res_a == {}, f"request A denied despite operator allow: {res_a}"
        assert res_b == {}, f"request B denied despite operator allow: {res_b}"

    async def test_concurrent_mixed_verdicts_reach_correct_waiter(self):
        """Cross-delivery must be impossible: A allowed, B denied — each
        waiter gets its own verdict regardless of tap order."""
        from hooks import make_engagement_permission_relay
        eid = "8" * 32
        reg = _FakeRegistry({eid: _FakeRecord()})
        tg = _FakeTelegramChannel()
        q = asyncio.Queue()
        hook = make_engagement_permission_relay(
            engagement_registry=reg, telegram_channel=tg,
            queues={eid: q}, timeout_s=2.0,
        )

        def payload(rid):
            return {"tool_name": "WebFetch", "tool_input": {"url": "https://x"},
                    "cwd": f"/data/engagements/{eid}", "tool_use_id": rid}

        t_a = asyncio.create_task(hook(payload("rid_A"), None, {}))
        t_b = asyncio.create_task(hook(payload("rid_B"), None, {}))
        await asyncio.sleep(0.05)

        await q.put({"request_id": "rid_B", "verdict": "deny"})
        await q.put({"request_id": "rid_A", "verdict": "allow"})

        res_a, res_b = await asyncio.wait_for(
            asyncio.gather(t_a, t_b), timeout=1.0,
        )
        assert res_a == {}, f"A should be allowed: {res_a}"
        assert res_b["hookSpecificOutput"]["permissionDecision"] == "deny"


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
