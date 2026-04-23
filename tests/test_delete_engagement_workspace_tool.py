"""Tests for delete_engagement_workspace MCP tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


def _make_ws(tmp_path: Path, eid: str, status: str = "COMPLETED"):
    ws = tmp_path / eid
    ws.mkdir()
    (ws / "x.txt").write_text("y", encoding="utf-8")
    (ws / ".casa-meta.json").write_text(json.dumps({
        "engagement_id": eid, "status": status,
    }), encoding="utf-8")
    return ws


async def test_delete_terminal_workspace(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import delete_engagement_workspace
    from engagement_registry import EngagementRegistry, EngagementRecord

    _make_ws(tmp_path, "eng-done")
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    reg._records["eng-done"] = EngagementRecord(
        id="eng-done", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="completed", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=0.0, sdk_session_id=None, origin={}, task="t",
    )
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "eng-done"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "ok"
    assert not (tmp_path / "eng-done").exists()


async def test_refuses_undergoing_without_force(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import delete_engagement_workspace
    from engagement_registry import EngagementRegistry, EngagementRecord

    _make_ws(tmp_path, "eng-running", status="UNDERGOING")
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    reg._records["eng-running"] = EngagementRecord(
        id="eng-running", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "eng-running"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "refused"
    assert (tmp_path / "eng-running").exists()  # untouched


async def test_unknown_engagement_error(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import delete_engagement_workspace
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "nope"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "unknown_engagement"
