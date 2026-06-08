"""Schema backstop tests for config_sync.reconcile.

A kept-live file invalid against the new schema must be force-overwritten
with the default so casa always boots (spec §3.4).
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


class _FakeGit:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.snapshots: list[str] = []
    def snapshot(self, message: str) -> str | None:
        if not self.available:
            return None
        self.snapshots.append(message)
        return f"SHA{len(self.snapshots)}"
    def head(self) -> str | None:
        return "HEAD" if self.available else None


def test_backstop_forces_invalid_kept_live_file(tmp_path: Path) -> None:
    # Image ships a tightened schema but UNCHANGED default → 'keep live' cell,
    # yet live is now schema-invalid. Backstop must force the default.
    rel = "agents/assistant/runtime.yaml"
    _write(tmp_path / "baseline", rel, "OLD")
    _write(tmp_path / "live", rel, "STALE_INVALID")  # edited, now invalid
    _write(tmp_path / "defaults", rel, "OLD")        # image unchanged → matrix keeps live

    def validate(r: str) -> str | None:
        # Only the live STALE_INVALID content is invalid.
        if r == rel and (tmp_path / "live" / r).read_text() == "STALE_INVALID":
            return f"{r}: schema violation at (root): missing required field"
        return None

    git = _FakeGit()
    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v9.9.9",
        git=git, validate=validate,
    )
    assert (tmp_path / "live" / rel).read_text() == "OLD"     # forced to default
    assert [e["path"] for e in report.schema_forced] == [rel]
    assert report.conflicts == []
    assert sum("pre-sync snapshot" in m for m in git.snapshots) == 1  # pre-sync commit taken


def test_backstop_noop_when_all_valid(tmp_path: Path) -> None:
    rel = "agents/assistant/runtime.yaml"
    _write(tmp_path / "baseline", rel, "OK")
    _write(tmp_path / "live", rel, "OK")
    _write(tmp_path / "defaults", rel, "OK")
    report = config_sync.reconcile(
        defaults_dir=tmp_path / "defaults", config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline", image_version="v9.9.9",
        git=_FakeGit(), validate=lambda r: None,
    )
    assert report.schema_forced == []
