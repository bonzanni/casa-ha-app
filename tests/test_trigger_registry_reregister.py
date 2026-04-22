"""Tests for TriggerRegistry.reregister_for (Plan 3)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestReregisterFor:
    def _make_registry(self):
        from trigger_registry import TriggerRegistry
        scheduler = MagicMock()
        scheduler.get_jobs = MagicMock(return_value=[])
        app = MagicMock()
        app.router = MagicMock()
        app.router.add_post = MagicMock()
        bus = MagicMock()
        return TriggerRegistry(scheduler=scheduler, app=app, bus=bus), scheduler, app

    def test_reregister_clears_existing_jobs(self):
        from config import TriggerSpec
        reg, scheduler, _app = self._make_registry()
        t1 = TriggerSpec(
            name="t1", type="interval", minutes=5,
            channel="telegram", prompt="p",
        )
        reg.register_agent("assistant", [t1], channels=["telegram"])
        assert "assistant:t1" in reg._seen_job_ids

        t2 = TriggerSpec(
            name="t2", type="cron", schedule="0 9 * * *",
            channel="telegram", prompt="q",
        )
        reg.reregister_for("assistant", [t2], channels=["telegram"])
        assert "assistant:t1" not in reg._seen_job_ids
        assert "assistant:t2" in reg._seen_job_ids
        scheduler.remove_job.assert_any_call("assistant:t1")

    def test_reregister_fails_closed_on_conflict(self):
        from config import TriggerSpec
        from trigger_registry import TriggerError
        reg, _sched, _app = self._make_registry()
        t1 = TriggerSpec(
            name="t", type="interval", minutes=5,
            channel="telegram", prompt="p",
        )
        reg.register_agent("assistant", [t1], channels=["telegram"])

        bad = TriggerSpec(
            name="bad", type="cron", schedule="not-a-cron",
            channel="telegram", prompt="q",
        )
        with pytest.raises(TriggerError):
            reg.reregister_for("assistant", [bad], channels=["telegram"])
        assert "assistant:t" not in reg._seen_job_ids
        assert "assistant:bad" not in reg._seen_job_ids

    def test_reregister_unknown_role_is_noop_then_registers(self):
        from config import TriggerSpec
        reg, _sched, _app = self._make_registry()
        t = TriggerSpec(
            name="t", type="interval", minutes=5,
            channel="telegram", prompt="p",
        )
        reg.reregister_for("assistant", [t], channels=["telegram"])
        assert "assistant:t" in reg._seen_job_ids
