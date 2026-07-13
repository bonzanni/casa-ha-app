"""§3.10 notify_plugin_health: operator DM on NEW fingerprints via the
deterministic telegram outbound target; mark-notified ONLY on successful
enqueue (Telegram-down retries next boot/mutation)."""
from __future__ import annotations

import pytest

import casa_core
import plugin_health
from plugin_registry import PluginIssue

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class _FakeBus:
    def __init__(self, has_telegram=True, raise_on_notify=False):
        self.queues = {"telegram": None} if has_telegram else {}
        self.raise_on_notify = raise_on_notify
        self.sent = []

    async def notify(self, msg):
        if self.raise_on_notify:
            raise RuntimeError("telegram down")
        self.sent.append(msg)


def _report(tmp_path, *issues):
    p = tmp_path / "plugin-health.json"
    plugin_health.write_report(issues=list(issues), warnings=[], path=p)
    return p


async def test_new_fingerprints_send_and_mark(tmp_path):
    p = _report(tmp_path, PluginIssue("lesina-invoice", "specialist:finance",
                                      "resolve", "corrupt_artifact", "a" * 64))
    bus = _FakeBus()
    await casa_core.notify_plugin_health(bus, path=str(p))
    assert len(bus.sent) == 1
    assert bus.sent[0].source == "plugin_health"
    assert "lesina-invoice" in bus.sent[0].content
    # Marked → no new fingerprints remain.
    assert plugin_health.new_fingerprints(plugin_health.load_report(p)) == []


async def test_already_notified_no_dm(tmp_path):
    p = _report(tmp_path, PluginIssue("p", "specialist:finance", "resolve",
                                      "artifact_missing", None))
    await casa_core.notify_plugin_health(_FakeBus(), path=str(p))
    bus2 = _FakeBus()
    await casa_core.notify_plugin_health(bus2, path=str(p))   # report unchanged
    assert bus2.sent == []


async def test_send_failure_not_marked_retries(tmp_path):
    p = _report(tmp_path, PluginIssue("p", "specialist:finance", "resolve",
                                      "artifact_missing", None))
    await casa_core.notify_plugin_health(
        _FakeBus(raise_on_notify=True), path=str(p))
    # Not marked → a later (working) call still delivers.
    assert plugin_health.new_fingerprints(plugin_health.load_report(p))
    bus = _FakeBus()
    await casa_core.notify_plugin_health(bus, path=str(p))
    assert len(bus.sent) == 1


async def test_no_telegram_queue_defers(tmp_path):
    p = _report(tmp_path, PluginIssue("p", "specialist:finance", "resolve",
                                      "artifact_missing", None))
    await casa_core.notify_plugin_health(_FakeBus(has_telegram=False), path=str(p))
    # Not marked (telegram down at boot) → retried next time.
    assert plugin_health.new_fingerprints(plugin_health.load_report(p))


async def test_empty_report_is_noop(tmp_path):
    p = _report(tmp_path)          # no issues
    bus = _FakeBus()
    await casa_core.notify_plugin_health(bus, path=str(p))
    assert bus.sent == []


async def test_warning_only_change_fires_dm(tmp_path):
    """Sol #17: a warning-only report (e.g. legacy_provenance from offline-adopt)
    must fire the operator DM — new_fingerprints now spans warnings, and the DM
    body lists them (not a vacuous '0 items')."""
    path = tmp_path / "health.json"
    w = PluginIssue(name="lesina", target=None, stage="migration",
                    reason_code="legacy_provenance")
    plugin_health.write_report(issues=[], warnings=[w], path=path)
    report = plugin_health.load_report(path)
    assert len(plugin_health.new_fingerprints(report)) == 1

    bus = _FakeBus()
    await casa_core.notify_plugin_health(bus, path=str(path))
    assert len(bus.sent) == 1
    assert "legacy_provenance" in bus.sent[0].content
    # marked notified → second call is a no-op (deduped).
    await casa_core.notify_plugin_health(bus, path=str(path))
    assert len(bus.sent) == 1
