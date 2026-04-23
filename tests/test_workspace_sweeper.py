"""Tests for drivers.workspace._sweep_workspaces."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


def _write_meta(ws: Path, *, status: str, retention_iso: str | None = None):
    meta = {
        "engagement_id": ws.name,
        "executor_type": "hello-driver",
        "status": status,
        "created_at": "2026-04-01T00:00:00Z",
        "finished_at": None,
        "retention_until": retention_iso,
    }
    (ws / ".casa-meta.json").write_text(json.dumps(meta), encoding="utf-8")


async def test_sweeper_deletes_terminal_expired(tmp_path):
    from drivers.workspace import _sweep_workspaces

    ws1 = tmp_path / "eng-done-old"
    ws1.mkdir()
    _write_meta(ws1, status="COMPLETED", retention_iso="2020-01-01T00:00:00Z")

    await _sweep_workspaces(engagements_root=str(tmp_path))

    assert not ws1.exists(), "expired completed workspace should be deleted"


async def test_sweeper_keeps_active_and_in_grace(tmp_path):
    from drivers.workspace import _sweep_workspaces

    ws_active = tmp_path / "eng-active"
    ws_active.mkdir()
    _write_meta(ws_active, status="UNDERGOING")

    ws_grace = tmp_path / "eng-grace"
    ws_grace.mkdir()
    # Far-future retention.
    _write_meta(ws_grace, status="COMPLETED", retention_iso="2099-01-01T00:00:00Z")

    await _sweep_workspaces(engagements_root=str(tmp_path))

    assert ws_active.exists()
    assert ws_grace.exists()


async def test_sweeper_skips_missing_meta(tmp_path):
    from drivers.workspace import _sweep_workspaces

    ws_orphan = tmp_path / "eng-no-meta"
    ws_orphan.mkdir()
    # no .casa-meta.json

    await _sweep_workspaces(engagements_root=str(tmp_path))

    assert ws_orphan.exists(), "no meta should leave alone (user prunes explicitly)"


async def test_sweeper_skips_terminal_without_retention(tmp_path):
    """Terminal state but retention_until=None is a bug; sweeper logs and skips."""
    from drivers.workspace import _sweep_workspaces

    ws = tmp_path / "eng-bug"
    ws.mkdir()
    _write_meta(ws, status="CANCELLED", retention_iso=None)

    await _sweep_workspaces(engagements_root=str(tmp_path))

    assert ws.exists()


async def test_sweeper_tolerates_missing_root(tmp_path):
    """Root not existing should be a silent no-op."""
    from drivers.workspace import _sweep_workspaces
    await _sweep_workspaces(engagements_root=str(tmp_path / "does-not-exist"))
