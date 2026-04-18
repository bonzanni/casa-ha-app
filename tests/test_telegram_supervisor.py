"""Unit tests for the Telegram reconnect supervisor (spec 5.2 §4)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from channels.telegram_supervisor import ReconnectSupervisor


pytestmark = pytest.mark.asyncio


async def _drain_pending() -> None:
    """Yield enough to let queued tasks run."""
    for _ in range(5):
        await asyncio.sleep(0)


class TestTriggerAndRebuild:
    async def test_trigger_invokes_rebuild_once_on_success(self):
        rebuild = AsyncMock()
        sup = ReconnectSupervisor(
            rebuild_fn=rebuild, logger=logging.getLogger("t"),
            initial_ms=1, cap_ms=4,
        )
        sup.start()
        try:
            sup.trigger("test")
            await _drain_pending()
            assert rebuild.await_count == 1
        finally:
            await sup.stop()

    async def test_no_rebuild_without_trigger(self):
        rebuild = AsyncMock()
        sup = ReconnectSupervisor(
            rebuild_fn=rebuild, logger=logging.getLogger("t"),
            initial_ms=1, cap_ms=4,
        )
        sup.start()
        try:
            await _drain_pending()
            assert rebuild.await_count == 0
        finally:
            await sup.stop()


class TestBackoffOnFailure:
    async def test_rebuild_retries_after_failure(self):
        rebuild = AsyncMock(side_effect=[RuntimeError("x"), None])
        sup = ReconnectSupervisor(
            rebuild_fn=rebuild, logger=logging.getLogger("t"),
            initial_ms=1, cap_ms=4,
        )
        sup.start()
        try:
            sup.trigger("test")
            # Wait long enough for two attempts with initial_ms=1 sleep.
            for _ in range(100):
                await asyncio.sleep(0.005)
                if rebuild.await_count >= 2:
                    break
            assert rebuild.await_count == 2
        finally:
            await sup.stop()

    async def test_unbounded_retry_until_success(self):
        # Three failures then success — supervisor does not give up.
        rebuild = AsyncMock(side_effect=[
            RuntimeError("a"), RuntimeError("b"), RuntimeError("c"), None,
        ])
        sup = ReconnectSupervisor(
            rebuild_fn=rebuild, logger=logging.getLogger("t"),
            initial_ms=1, cap_ms=4,
        )
        sup.start()
        try:
            sup.trigger("test")
            for _ in range(200):
                await asyncio.sleep(0.005)
                if rebuild.await_count >= 4:
                    break
            assert rebuild.await_count == 4
        finally:
            await sup.stop()


class TestLogOnceSemantics:
    async def test_single_error_log_per_outage(self, caplog):
        caplog.set_level(logging.DEBUG, logger="sup-test")
        rebuild = AsyncMock(side_effect=[
            RuntimeError("a"), RuntimeError("b"), None,
        ])
        sup = ReconnectSupervisor(
            rebuild_fn=rebuild, logger=logging.getLogger("sup-test"),
            initial_ms=1, cap_ms=4,
        )
        sup.start()
        try:
            sup.trigger("reason-one")
            for _ in range(200):
                await asyncio.sleep(0.005)
                if rebuild.await_count >= 3:
                    break
            error_records = [
                r for r in caplog.records
                if r.levelno == logging.ERROR and r.name == "sup-test"
            ]
            assert len(error_records) == 1, [r.message for r in error_records]
        finally:
            await sup.stop()

    async def test_single_info_log_on_recovery(self, caplog):
        caplog.set_level(logging.DEBUG, logger="sup-test")
        rebuild = AsyncMock(side_effect=[RuntimeError("a"), None])
        sup = ReconnectSupervisor(
            rebuild_fn=rebuild, logger=logging.getLogger("sup-test"),
            initial_ms=1, cap_ms=4,
        )
        sup.start()
        try:
            sup.trigger("reason-one")
            for _ in range(200):
                await asyncio.sleep(0.005)
                if rebuild.await_count >= 2:
                    break
            info_records = [
                r for r in caplog.records
                if r.levelno == logging.INFO and r.name == "sup-test"
            ]
            assert len(info_records) == 1, [r.message for r in info_records]
            assert "recover" in info_records[0].message.lower()
        finally:
            await sup.stop()

    async def test_successful_first_rebuild_emits_no_error_no_info(self, caplog):
        caplog.set_level(logging.DEBUG, logger="sup-test")
        rebuild = AsyncMock()
        sup = ReconnectSupervisor(
            rebuild_fn=rebuild, logger=logging.getLogger("sup-test"),
            initial_ms=1, cap_ms=4,
        )
        sup.start()
        try:
            sup.trigger("first")
            await _drain_pending()
            error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
            info_records = [
                r for r in caplog.records
                if r.levelno == logging.INFO and r.name == "sup-test"
            ]
            assert error_records == []
            assert info_records == []
        finally:
            await sup.stop()

    async def test_log_state_resets_between_outages(self, caplog):
        caplog.set_level(logging.DEBUG, logger="sup-test")
        rebuild = AsyncMock(side_effect=[
            RuntimeError("a"), None,  # first outage: 1 fail then recover
            RuntimeError("b"), None,  # second outage: 1 fail then recover
        ])
        sup = ReconnectSupervisor(
            rebuild_fn=rebuild, logger=logging.getLogger("sup-test"),
            initial_ms=1, cap_ms=4,
        )
        sup.start()
        try:
            sup.trigger("outage-1")
            for _ in range(200):
                await asyncio.sleep(0.005)
                if rebuild.await_count >= 2:
                    break
            sup.trigger("outage-2")
            for _ in range(200):
                await asyncio.sleep(0.005)
                if rebuild.await_count >= 4:
                    break
            error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
            info_records = [
                r for r in caplog.records
                if r.levelno == logging.INFO and r.name == "sup-test"
            ]
            assert len(error_records) == 2
            assert len(info_records) == 2
        finally:
            await sup.stop()


class TestLifecycle:
    async def test_stop_cancels_in_flight_retry(self):
        # Rebuild keeps failing; stop must exit cleanly mid-backoff.
        rebuild = AsyncMock(side_effect=RuntimeError("forever"))
        sup = ReconnectSupervisor(
            rebuild_fn=rebuild, logger=logging.getLogger("t"),
            initial_ms=100, cap_ms=200,  # long-ish so we stop mid-sleep
        )
        sup.start()
        sup.trigger("forever")
        await asyncio.sleep(0.02)  # let one attempt run
        await sup.stop()  # must not raise, must not hang
        # One more drain — nothing should be scheduled after stop.
        count_at_stop = rebuild.await_count
        await asyncio.sleep(0.3)
        assert rebuild.await_count == count_at_stop

    async def test_stop_before_start_is_safe(self):
        sup = ReconnectSupervisor(
            rebuild_fn=AsyncMock(), logger=logging.getLogger("t"),
            initial_ms=1, cap_ms=4,
        )
        await sup.stop()  # no-op, must not raise

    async def test_double_start_is_safe(self):
        rebuild = AsyncMock()
        sup = ReconnectSupervisor(
            rebuild_fn=rebuild, logger=logging.getLogger("t"),
            initial_ms=1, cap_ms=4,
        )
        sup.start()
        sup.start()  # idempotent — must not spawn a second task
        try:
            sup.trigger("test")
            await _drain_pending()
            assert rebuild.await_count == 1
        finally:
            await sup.stop()
