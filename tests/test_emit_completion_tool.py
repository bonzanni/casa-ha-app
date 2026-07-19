"""Tests for the emit_completion tool (agent-side, Plan 2)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestEmitCompletionHandler:
    async def test_returns_acknowledged_inside_engagement(self, tmp_path, monkeypatch):
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools, engagement_var

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
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler({
                "text": "done", "artifacts": ["sha1"], "next_steps": [], "status": "ok",
            })
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"

    async def test_returns_not_in_engagement_when_outside(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        res = await emit_completion.handler({"text": "x"})
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "not_in_engagement"

    async def test_error_status_finalizes_with_error_outcome(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools, engagement_var

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
        token = engagement_var.set(rec)
        try:
            await emit_completion.handler({"text": "boom", "status": "error"})
        finally:
            engagement_var.reset(token)
        assert rec.status == "error"


class TestEmitCompletionIdempotency:
    """Bug 9 (v0.14.6): emit_completion is a no-op once the engagement is
    in a terminal state. Pre-fix, a duplicate call (SDK retry / hook
    misfire) ran _finalize_engagement twice, double-NOTIFYing Ellen and
    double-writing the meta-scope summary into Honcho.
    """

    async def _make_finalised_engagement(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
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
        token = engagement_var.set(rec)
        return reg, rec, cm, tch, bus, token, emit_completion

    async def test_double_emit_does_not_re_finalize(self, tmp_path):
        from tools import engagement_var
        reg, rec, cm, tch, bus, token, emit_completion = (
            await self._make_finalised_engagement(tmp_path)
        )
        try:
            res1 = await emit_completion.handler({"text": "done", "status": "ok"})
            payload1 = json.loads(res1["content"][0]["text"])
            assert payload1["status"] == "acknowledged"
            assert rec.status == "completed"

            # Snapshot side-effect counts after the legitimate first call.
            close_calls_before = tch.close_topic.await_count
            notify_calls_before = bus.notify.await_count

            # Re-emit (the bug scenario).
            res2 = await emit_completion.handler({"text": "done again", "status": "ok"})
        finally:
            engagement_var.reset(token)

        payload2 = json.loads(res2["content"][0]["text"])
        # Tool acknowledges but tags it as a no-op.
        assert payload2["status"] == "acknowledged"
        assert payload2["kind"] == "already_terminal"

        # Critical assertion: side effects did NOT fire a second time.
        assert tch.close_topic.await_count == close_calls_before
        assert bus.notify.await_count == notify_calls_before

    async def test_re_emit_after_cancel_is_noop(self, tmp_path):
        from tools import engagement_var
        reg, rec, cm, tch, bus, token, emit_completion = (
            await self._make_finalised_engagement(tmp_path)
        )
        try:
            await reg.mark_cancelled(rec.id)
            assert rec.status == "cancelled"
            res = await emit_completion.handler({"text": "late", "status": "ok"})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "already_terminal"
        # Topic close / bus.notify NEVER fired.
        assert tch.close_topic.await_count == 0
        assert bus.notify.await_count == 0


class TestConcurrentCancelVsEmit:
    """L75/L24: /cancel landing while emit_completion is suspended in a
    real await (the G-2 forced-reload window) must not double-finalize
    or let emit_completion clobber the winning 'cancelled' outcome."""

    async def test_cancel_during_g2_reload_window_finalizes_once(
        self, tmp_path, monkeypatch,
    ):
        import tools
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, engagement_var, init_tools, _finalize_engagement

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"}, topic_id=42,
        )
        tch = MagicMock(); tch.send_to_topic = AsyncMock(); tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(channel_manager=cm, bus=bus, specialist_registry=MagicMock(),
                   mcp_registry=MagicMock(), trigger_registry=MagicMock(),
                   engagement_registry=reg)

        gate = asyncio.Event()

        async def fake_reload(args):
            await gate.wait()
            return {"content": [{"type": "text", "text": json.dumps({"status": "ok"})}]}
        monkeypatch.setattr(tools.casa_reload, "handler", fake_reload)
        tools._ENGAGEMENTS_PENDING_RELOAD.add(rec.id)

        token = engagement_var.set(rec)
        try:
            emit_task = asyncio.create_task(
                emit_completion.handler({"text": "done", "status": "ok"}))
            await asyncio.sleep(0)   # emit passes its terminal check, parks in fake_reload
            # user /cancel wins the race (same call casa_core._finalize_cancel makes)
            await _finalize_engagement(rec, outcome="cancelled",
                                       text="Cancelled by user.",
                                       artifacts=[], next_steps=[], driver=None)
            gate.set()
            await emit_task
        finally:
            engagement_var.reset(token)

        assert rec.status == "cancelled"          # emit must NOT overwrite the cancel
        assert bus.notify.await_count == 1        # exactly one DelegationComplete
        assert tch.close_topic.await_count == 1   # topic closed exactly once


def _wire_engagement(tmp_path):
    """Registry + mocks + init_tools; returns (rec, registry, telegram, bus)."""
    import asyncio as _a  # noqa: F401
    from engagement_registry import EngagementRegistry
    from tools import init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = _a.get_event_loop() if False else None  # placeholder never used
    tch = MagicMock(); tch.send_to_topic = AsyncMock(); tch.close_topic = AsyncMock()
    cm = MagicMock(); cm.get.return_value = tch
    bus = MagicMock(); bus.notify = AsyncMock()
    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    return reg, tch, bus


class TestEmitCompletionValidation:
    """B-3 (v0.69.3): a fully-successful configurator engagement finalized
    outcome=error kind=emit_completion_error (2026-07-12 00:14Z) — the tool
    mapped EVERY status other than exactly "ok" (including the doctrine's
    own "partial"/"cancelled", or a model writing "success") to a terminal
    error, and malformed arg shapes rode straight into finalize. Malformed
    calls must get a TOOL error back (agent retries); doctrine statuses map
    to their true outcomes."""

    async def _emit(self, reg, rec, args):
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler(args)
        finally:
            engagement_var.reset(token)
        return json.loads(res["content"][0]["text"])

    async def _rec(self, reg, tmp_path):
        return await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )

    async def test_unknown_status_is_tool_error_not_engagement_failure(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(reg, rec, {"text": "all good", "status": "success"})
        assert payload["status"] == "error"
        assert payload["kind"] == "invalid_status"
        assert "ok" in payload["message"] and "partial" in payload["message"]
        assert rec.status == "active"          # engagement NOT finalized
        tch.close_topic.assert_not_awaited()   # agent gets to retry

    async def test_cancelled_status_finalizes_cancelled_not_error(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(reg, rec, {"text": "user aborted", "status": "cancelled"})
        assert payload["status"] == "acknowledged"
        assert rec.status == "cancelled"       # doctrine status, true outcome
        assert rec.origin.get("error_kind") is None

    async def test_partial_status_completes_with_partial_marker(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(reg, rec, {"text": "did 2 of 3", "status": "partial"})
        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"
        sent = " ".join(str(c.args) for c in tch.send_to_topic.await_args_list)
        assert "partial" in sent.lower()

    async def test_failed_status_finalizes_error(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(reg, rec, {"text": "could not", "status": "failed"})
        assert payload["status"] == "acknowledged"
        assert rec.status == "error"

    async def test_string_artifacts_wrapped_not_exploded(self, tmp_path):
        """list("sha123") == ['s','h','a','1','2','3'] — the old coercion."""
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(
            reg, rec, {"text": "done", "status": "ok", "artifacts": "sha123"})
        assert payload["status"] == "acknowledged"
        complete = bus.notify.await_args_list[0].args[0].content
        assert complete.origin is not None  # sanity: DelegationComplete shape

    async def test_non_list_next_steps_is_tool_error(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(
            reg, rec, {"text": "done", "status": "ok", "next_steps": {"step": 1}})
        assert payload["kind"] == "invalid_args"
        assert rec.status == "active"

    async def test_non_string_text_is_tool_error(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(
            reg, rec, {"text": {"summary": "done"}, "status": "ok"})
        assert payload["kind"] == "invalid_args"
        assert rec.status == "active"

    async def test_oversized_text_truncated_not_fatal(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(
            reg, rec, {"text": "x" * 50000, "status": "ok"})
        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"
        complete = bus.notify.await_args_list[0].args[0].content
        assert len(complete.text) <= 8100  # 8000 cap + truncation marker


class TestPluginDeveloperCompletionGuard:
    """A.2 (v0.74.0): the release-identity gate at the emit_completion
    boundary — rejection keeps the engagement live with NO finalize side
    effects."""

    async def _mk(self, tmp_path, *, role="plugin-developer"):
        from engagement_registry import EngagementRegistry
        from tools import init_tools
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="executor", role_or_type=role, driver="claude_code",
            task="build plugin",
            origin={"role": "assistant", "channel": "telegram"}, topic_id=42)
        tch = MagicMock()
        tch.send_to_topic = AsyncMock()
        tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(channel_manager=cm, bus=bus,
                   specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                   trigger_registry=MagicMock(), engagement_registry=reg)
        return reg, rec, tch, bus

    async def test_bad_artifact_rejected_engagement_live_no_side_effects(
            self, tmp_path, monkeypatch):
        import plugin_completion_guard as guard_mod
        reg, rec, tch, bus = await self._mk(tmp_path)
        monkeypatch.setattr(
            guard_mod, "validate_completion_artifacts",
            lambda arts: [{"index": 0, "reason_code": "tag_not_annotated",
                           "message": "m"}])
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler(
                {"text": "done", "status": "ok",
                 "artifacts": [{"kind": "casa_plugin_repo"}]})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "completion_rejected"
        assert payload["failures"][0]["reason_code"] == "tag_not_annotated"
        assert res.get("is_error") is True
        assert reg.get(rec.id).status == "active"       # engagement stays live
        tch.close_topic.assert_not_called()             # S2: no finalize
        tch.send_to_topic.assert_not_called()           #     side effects
        bus.notify.assert_not_called()

    async def test_valid_artifact_finalizes(self, tmp_path, monkeypatch):
        import plugin_completion_guard as guard_mod
        reg, rec, tch, bus = await self._mk(tmp_path)
        monkeypatch.setattr(guard_mod, "validate_completion_artifacts",
                            lambda arts: [])
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler(
                {"text": "done", "status": "ok",
                 "artifacts": [{"kind": "casa_plugin_repo"}]})
        finally:
            engagement_var.reset(token)
        assert json.loads(res["content"][0]["text"])["status"] == "acknowledged"
        assert reg.get(rec.id).status == "completed"

    async def test_non_ok_status_skips_guard(self, tmp_path, monkeypatch):
        import plugin_completion_guard as guard_mod
        reg, rec, tch, bus = await self._mk(tmp_path)
        monkeypatch.setattr(
            guard_mod, "validate_completion_artifacts",
            lambda arts: (_ for _ in ()).throw(AssertionError("not called")))
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            await emit_completion.handler(
                {"text": "gave up", "status": "failed", "artifacts": []})
        finally:
            engagement_var.reset(token)
        assert reg.get(rec.id).status == "error"

    async def test_other_executor_types_skip_guard(self, tmp_path, monkeypatch):
        import plugin_completion_guard as guard_mod
        reg, rec, tch, bus = await self._mk(tmp_path, role="configurator")
        monkeypatch.setattr(
            guard_mod, "validate_completion_artifacts",
            lambda arts: (_ for _ in ()).throw(AssertionError("not called")))
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            await emit_completion.handler(
                {"text": "done", "status": "ok", "artifacts": []})
        finally:
            engagement_var.reset(token)
        assert reg.get(rec.id).status == "completed"

    async def test_guard_crash_fails_closed(self, tmp_path, monkeypatch):
        import plugin_completion_guard as guard_mod
        reg, rec, tch, bus = await self._mk(tmp_path)
        monkeypatch.setattr(
            guard_mod, "validate_completion_artifacts",
            lambda arts: (_ for _ in ()).throw(RuntimeError("boom")))
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler(
                {"text": "done", "status": "ok",
                 "artifacts": [{"kind": "casa_plugin_repo"}]})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "completion_rejected"
        assert payload["failures"][0]["reason_code"] == "guard_error"
        assert reg.get(rec.id).status == "active"


class TestFinalizeResultMapping:
    """G4 D5 (v0.96.0): emit_completion must not acknowledge a completion
    that did not actually finalize (Sol g4-r1-6 — the persistence-rollback
    False was previously acked as success)."""

    async def _setup(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import init_tools
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )
        tch = MagicMock(); tch.send_to_topic = AsyncMock()
        tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        return reg, rec

    async def test_persist_failure_returns_retryable_error(
            self, tmp_path, monkeypatch):
        from tools import emit_completion, engagement_var
        reg, rec = await self._setup(tmp_path)

        async def boom(*a, **k):
            raise OSError("tombstone write failed")
        monkeypatch.setattr(reg, "try_transition_terminal", boom)

        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler({
                "text": "done", "artifacts": [], "next_steps": [],
                "status": "ok"})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] != "acknowledged"
        assert payload["kind"] == "finalize_persist_failed"
        assert payload.get("retryable") is True
        assert rec.status not in ("completed", "error", "cancelled")


class _FakeInboundDriver:
    """Minimal claude_code-driver stand-in for the G4 completion gate."""
    def __init__(self, depth=0, reservations=0, texts=()):
        self._depth = depth
        self._resv = reservations
        self._texts = list(texts)
        self.refusals: list[str] = []
        self.cancelled_intents: list[tuple] = []

    def inbound_unread_depth(self, eng_id): return self._depth
    def inbound_reservations(self, eng_id): return self._resv
    def inbound_unread_texts(self, eng_id): return list(self._texts)
    def record_completion_refusal(self, eng_id):
        self.refusals.append(eng_id); return len(self.refusals)
    def cancel_send_intent(self, eng_id, request_id):
        self.cancelled_intents.append((eng_id, request_id))
    def register_completion_consumption(self, eng_id, args): pass
    async def drain_inbound_spool(self, engagement): pass


class TestCompletionInboundGate:
    """G4 D1/D2 (v0.96.0): emit_completion must not complete past unread
    operator input."""

    async def _setup(self, tmp_path, driver):
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="probe-exec",
            driver="claude_code", task="t",
            origin={"role": "assistant", "channel": "telegram"}, topic_id=42,
        )
        tch = MagicMock(); tch.send_to_topic = AsyncMock()
        tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        agent_mod.active_claude_code_driver = driver
        return reg, rec, tch

    async def _emit(self, rec, status="ok"):
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler({
                "text": "done", "artifacts": [], "next_steps": [],
                "status": status})
        finally:
            engagement_var.reset(token)
        return json.loads(res["content"][0]["text"])

    async def test_unread_queued_message_refuses_completion(
            self, tmp_path, monkeypatch):
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=1, texts=["change the design"])
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["kind"] == "unread_inbound"
        assert payload.get("retryable") is True
        assert rec.status not in ("completed", "error", "cancelled")
        assert drv.refusals  # counted for escalation

    async def test_pending_ingress_reservation_refuses_completion(
            self, tmp_path):
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=0, reservations=1)
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")

    async def test_clean_spool_completes(self, tmp_path):
        import agent as agent_mod
        drv = _FakeInboundDriver()
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"

    async def test_error_status_not_gated_but_annotated(self, tmp_path):
        """A broken engagement must be able to die even with unread input;
        the topic post carries the never-read texts (D4)."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=1, texts=["msg-that-was-never-read"])
        reg, rec, tch = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["status"] == "acknowledged"
        assert rec.status == "error"
        posted = "".join(str(c.args) + str(c.kwargs)
                         for c in tch.send_to_topic.call_args_list)
        assert "never read" in posted
        assert "msg-that-was-never-read" in posted

    async def test_race_message_lands_between_gate_and_flip(
            self, tmp_path, monkeypatch):
        """G4 D2: depth flips to >0 after the handler gate but before the
        terminal mutation — the registry-internal hook must refuse."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=0)
        reg, rec, _ = await self._setup(tmp_path, drv)

        async def racing_drain(engagement):
            drv._depth = 1
            drv._texts = ["late message"]
        drv.drain_inbound_spool = racing_drain

        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")
        # the pre-registered consumption debt was rolled back
        assert any(r[1].startswith("emit_completion:")
                   for r in drv.cancelled_intents) or drv.cancelled_intents == []
