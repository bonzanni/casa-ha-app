"""L27 — list_engagement_workspaces' du-style tree walk must run off the loop.

The handler computed a recursive size for every workspace under
/data/engagements with a synchronous ``os.walk`` + per-file ``os.stat``
directly on the shared event loop. On boxes with retained claude_code
workspaces (cloned repos, node_modules) that froze every channel for
seconds. This pins that the walk now runs via ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _make_workspace(root: Path, eid: str) -> None:
    ws = root / eid
    ws.mkdir()
    (ws / "file.txt").write_text("hello", encoding="utf-8")
    (ws / ".casa-meta.json").write_text(json.dumps({
        "engagement_id": eid, "executor_type": "hello-driver",
        "status": "COMPLETED", "created_at": "2026-04-01T00:00:00Z",
        "finished_at": "2026-04-01T00:05:00Z",
        "retention_until": "2099-01-01T00:00:00Z",
    }), encoding="utf-8")


async def test_list_workspaces_scan_runs_off_event_loop(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import list_engagement_workspaces

    _make_workspace(tmp_path, "eng-a")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    loop_thread_id = threading.get_ident()
    walk_threads: set[int] = set()
    real_walk = tools_mod.os.walk

    def spy_walk(path, *args, **kwargs):
        walk_threads.add(threading.get_ident())
        return real_walk(path, *args, **kwargs)

    monkeypatch.setattr(tools_mod.os, "walk", spy_walk)

    result = await list_engagement_workspaces.handler({})
    payload = json.loads(result["content"][0]["text"])

    assert payload["workspaces"][0]["size_bytes"] > 0  # scan still happened
    assert walk_threads, "os.walk was never called"
    assert loop_thread_id not in walk_threads, (
        "os.walk ran on the event-loop thread — it must be offloaded via "
        "asyncio.to_thread"
    )
