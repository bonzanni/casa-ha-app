"""Tests for peek_engagement_workspace MCP tool (read-only inspection)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


def _seed(tmp_path: Path, eid: str):
    ws = tmp_path / eid
    ws.mkdir()
    (ws / "a.txt").write_text("hello world", encoding="utf-8")
    (ws / "nested").mkdir()
    (ws / "nested" / "b.txt").write_text("deep", encoding="utf-8")
    return ws


async def test_peek_returns_tree_when_no_path(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import peek_engagement_workspace

    _seed(tmp_path, "eng1")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await peek_engagement_workspace.handler(
        {"engagement_id": "eng1"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert "tree" in payload
    names = [n["name"] for n in payload["tree"]]
    assert "a.txt" in names
    assert "nested" in names


async def test_peek_returns_file_contents(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import peek_engagement_workspace

    _seed(tmp_path, "eng1")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await peek_engagement_workspace.handler(
        {"engagement_id": "eng1", "path": "a.txt"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["contents"] == "hello world"


async def test_peek_rejects_path_traversal(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import peek_engagement_workspace

    _seed(tmp_path, "eng1")
    # Secret file outside the workspace.
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await peek_engagement_workspace.handler(
        {"engagement_id": "eng1", "path": "../secret.txt"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "path_outside_workspace"


async def test_peek_caps_max_bytes(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import peek_engagement_workspace

    ws = tmp_path / "eng1"
    ws.mkdir()
    (ws / "big.txt").write_text("A" * 10000, encoding="utf-8")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await peek_engagement_workspace.handler(
        {"engagement_id": "eng1", "path": "big.txt", "max_bytes": 100},
    )
    payload = json.loads(result["content"][0]["text"])
    assert len(payload["contents"]) == 100


async def test_peek_unknown_engagement(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import peek_engagement_workspace
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await peek_engagement_workspace.handler(
        {"engagement_id": "nope"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "unknown_workspace"
