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


class _FakeGit:
    """Records snapshot() calls; returns deterministic SHAs."""
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.snapshots: list[str] = []
        self._head = "BASEHEAD"

    def snapshot(self, message: str) -> str | None:
        if not self.available:
            return None
        self.snapshots.append(message)
        self._head = f"SHA{len(self.snapshots)}"
        return self._head

    def head(self) -> str | None:
        return None if not self.available else self._head


def _no_schema(_rel: str) -> None:
    return None  # backstop: nothing is ever schema-invalid


def _run(tmp_path: Path, *, version: str = "v9.9.9", git=None, validate=_no_schema):
    return config_sync.reconcile(
        defaults_dir=tmp_path / "defaults",
        config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline",
        image_version=version,
        git=git or _FakeGit(),
        validate=validate,
    )


def test_create_seeds_missing_file(tmp_path: Path) -> None:
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "NEW")
    # no baseline, no live
    report = _run(tmp_path)
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "NEW"
    assert "agents/butler/voice.yaml" in report.updated


def test_untouched_tracks_image_change(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "OLD")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "OLD")     # == baseline (untouched)
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "NEW") # image changed
    report = _run(tmp_path)
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "NEW"
    assert "agents/butler/voice.yaml" in report.updated


def test_untouched_file_removed_from_defaults_is_deleted(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/old/runtime.yaml", "X")
    _write(tmp_path / "live", "agents/old/runtime.yaml", "X")        # untouched
    # defaults: file absent
    report = _run(tmp_path)
    assert not (tmp_path / "live/agents/old/runtime.yaml").exists()
    assert "agents/old/runtime.yaml" in report.deleted


def test_user_edit_kept_when_image_unchanged(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "OLD")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "USER")    # != baseline (edited)
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "OLD") # == baseline (image unchanged)
    report = _run(tmp_path)
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "USER"
    assert report.updated == [] and report.conflicts == []


def test_user_edit_kept_when_image_removes_file(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "OLD")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "USER")    # edited
    # defaults: absent
    report = _run(tmp_path)
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "USER"
    assert report.deleted == []


def test_converged_is_noop(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "OLD")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "NEW")     # edited
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "NEW") # live == new
    report = _run(tmp_path)
    assert report.updated == [] and report.conflicts == []


def test_adopt_mode_no_baseline_keeps_existing_live(tmp_path: Path) -> None:
    # First reconciler run on an existing deployment: baseline absent.
    _write(tmp_path / "live", "agents/butler/voice.yaml", "USER")
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "NEW") # differs from live
    git = _FakeGit()
    report = _run(tmp_path, git=git)
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "USER"  # NOT clobbered
    assert report.conflicts == [] and git.snapshots == []           # no overwrite, no pre-sync commit
