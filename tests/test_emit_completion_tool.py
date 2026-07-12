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
