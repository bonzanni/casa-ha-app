"""Operator-notification tests: config-sync report → bus message to Ellen."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import casa_core

pytestmark = pytest.mark.unit


class _FakeBus:
    def __init__(self) -> None:
        self.messages = []
    async def notify(self, msg) -> None:
        self.messages.append(msg)


def _report(path: Path, **over) -> None:
    base = {
        "image_version": "v1.2.3", "pre_sync_sha": "abc",
        "updated": [], "deleted": [], "conflicts": [],
        "schema_forced": [], "casabak": [], "notified": False,
    }
    base.update(over)
    path.write_text(json.dumps(base), encoding="utf-8")


async def test_notify_posts_when_overwrites_present(tmp_path: Path) -> None:
    rp = tmp_path / "config-sync-report.json"
    _report(rp, conflicts=[{"path": "agents/butler/voice.yaml", "pre_sync_sha": "abc"}])
    bus = _FakeBus()
    await casa_core.notify_config_sync(bus, "assistant", report_path=str(rp))
    assert len(bus.messages) == 1
    msg = bus.messages[0]
    assert msg.target == "assistant"
    assert "agents/butler/voice.yaml" in str(msg.content)
    # report marked notified to prevent duplicate on svc restart
    assert json.loads(rp.read_text())["notified"] is True


async def test_no_notify_when_no_overwrites(tmp_path: Path) -> None:
    rp = tmp_path / "config-sync-report.json"
    _report(rp, updated=["agents/butler/voice.yaml"])  # routine only
    bus = _FakeBus()
    await casa_core.notify_config_sync(bus, "assistant", report_path=str(rp))
    assert bus.messages == []


async def test_no_notify_when_already_notified(tmp_path: Path) -> None:
    rp = tmp_path / "config-sync-report.json"
    _report(rp, conflicts=[{"path": "x", "pre_sync_sha": "y"}], notified=True)
    bus = _FakeBus()
    await casa_core.notify_config_sync(bus, "assistant", report_path=str(rp))
    assert bus.messages == []


async def test_no_notify_when_report_absent(tmp_path: Path) -> None:
    bus = _FakeBus()
    await casa_core.notify_config_sync(bus, "assistant",
                                       report_path=str(tmp_path / "missing.json"))
    assert bus.messages == []
