"""Tests for the three-way /config default-sync reconciler (config_sync.py).

Spec: docs/superpowers/specs/2026-06-08-config-sync-reconciler-design.md.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config_sync

pytestmark = pytest.mark.unit


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_list_tree_files_only_scopes_agents_and_policies(tmp_path: Path) -> None:
    _write(tmp_path, "agents/butler/voice.yaml", "a")
    _write(tmp_path, "policies/disclosure.yaml", "b")
    _write(tmp_path, "cc-home/.claude/settings.json", "c")   # out of scope
    _write(tmp_path, "marketplace/x.json", "d")              # out of scope
    rels = config_sync._list_tree_files(tmp_path)
    assert rels == {"agents/butler/voice.yaml", "policies/disclosure.yaml"}


def test_list_tree_files_skips_git_and_casabak(tmp_path: Path) -> None:
    _write(tmp_path, "agents/butler/voice.yaml", "a")
    _write(tmp_path, "agents/.git/HEAD", "ref")              # never happens, defensive
    _write(tmp_path, "agents/butler/voice.yaml.casabak", "x")
    rels = config_sync._list_tree_files(tmp_path)
    assert rels == {"agents/butler/voice.yaml"}


def test_bytes_equal(tmp_path: Path) -> None:
    a = tmp_path / "a"; a.write_text("same", encoding="utf-8")
    b = tmp_path / "b"; b.write_text("same", encoding="utf-8")
    c = tmp_path / "c"; c.write_text("diff", encoding="utf-8")
    assert config_sync._bytes_equal(a, b) is True
    assert config_sync._bytes_equal(a, c) is False


def test_report_has_overwrites_and_json_roundtrip() -> None:
    r = config_sync.SyncReport(image_version="v9.9.9")
    assert r.has_overwrites() is False
    r.conflicts.append({"path": "agents/x/y.yaml", "pre_sync_sha": "abc"})
    assert r.has_overwrites() is True
    import json
    parsed = json.loads(r.to_json())
    assert parsed["image_version"] == "v9.9.9"
    assert parsed["conflicts"][0]["path"] == "agents/x/y.yaml"
