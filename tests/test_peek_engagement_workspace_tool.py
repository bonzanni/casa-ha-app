"""Tests for peek_engagement_workspace MCP tool (read-only inspection)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


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


async def test_peek_rejects_engagement_id_traversal(tmp_path, monkeypatch):
    """H15: engagement_id must not re-root the workspace. A secret seeded
    ABOVE the engagements root must never leak through '..', an absolute
    re-root, or an empty-path tree of a traversed location."""
    import tools as tools_mod
    from tools import peek_engagement_workspace

    # layout: tmp/data/engagements/eng1 (root), tmp/data/options.json (secret)
    data = tmp_path / "data"
    eng = data / "engagements"
    (eng / "eng1").mkdir(parents=True)
    (data / "options.json").write_text(
        '{"telegram_bot_token":"SECRET"}', encoding="utf-8")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(eng),
                        raising=False)

    # dot-dot traversal into /data
    r = await peek_engagement_workspace.handler(
        {"engagement_id": "..", "path": "options.json"})
    p = json.loads(r["content"][0]["text"])
    assert p["status"] == "error"
    assert "SECRET" not in json.dumps(p)

    # nested dot-dot traversal
    r = await peek_engagement_workspace.handler(
        {"engagement_id": "../../config", "path": "plugin-env.conf"})
    p = json.loads(r["content"][0]["text"])
    assert p["status"] == "error"

    # absolute re-root
    r = await peek_engagement_workspace.handler(
        {"engagement_id": str(data), "path": "options.json"})
    p = json.loads(r["content"][0]["text"])
    assert p["status"] == "error"

    # empty-path tree of a traversed location must not leak
    r = await peek_engagement_workspace.handler({"engagement_id": ".."})
    p = json.loads(r["content"][0]["text"])
    assert p["status"] == "error"

    # legit id still works
    (eng / "eng1" / "a.txt").write_text("hello", encoding="utf-8")
    r = await peek_engagement_workspace.handler(
        {"engagement_id": "eng1", "path": "a.txt"})
    assert json.loads(r["content"][0]["text"])["contents"] == "hello"
