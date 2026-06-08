"""__main__ wiring tests for config_sync: RealGit + validator + report write."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import config_sync

pytestmark = pytest.mark.unit


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def test_real_git_snapshot_commits_and_returns_sha(tmp_path: Path) -> None:
    repo = tmp_path / "cfg"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "agents").mkdir()
    (repo / "agents/x.yaml").write_text("v1", encoding="utf-8")
    g = config_sync.RealGit(repo)
    assert g.available is True
    sha = g.snapshot("snap one")
    assert sha and len(sha) >= 7
    # nothing dirty now → snapshot returns current head, no new commit
    head_before = g.head()
    sha2 = g.snapshot("snap two")
    assert sha2 == head_before


def test_real_git_unavailable_when_not_a_repo(tmp_path: Path) -> None:
    g = config_sync.RealGit(tmp_path / "nope")
    assert g.available is False
    assert g.snapshot("x") is None
    assert g.head() is None


def test_run_writes_report_and_reconciles(tmp_path: Path, monkeypatch) -> None:
    defaults = tmp_path / "defaults"
    live = tmp_path / "live"
    baseline = tmp_path / "baseline"
    report_path = tmp_path / "report.json"
    _write(defaults, "agents/butler/voice.yaml", "NEW")
    # validator that always passes (no schema concerns here)
    monkeypatch.setattr(config_sync, "_make_validator", lambda cfg: (lambda rel: None))
    rc = config_sync.run(
        defaults_dir=defaults, config_dir=live, baseline_dir=baseline,
        report_path=report_path, image_version="v1.2.3",
    )
    assert rc == 0
    assert (live / "agents/butler/voice.yaml").read_text() == "NEW"
    data = json.loads(report_path.read_text())
    assert data["image_version"] == "v1.2.3"
    assert "agents/butler/voice.yaml" in data["updated"]


def test_run_is_non_fatal_on_error(tmp_path: Path, monkeypatch) -> None:
    # Force reconcile to raise; run() must swallow and return 0.
    monkeypatch.setattr(config_sync, "reconcile",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = config_sync.run(
        defaults_dir=tmp_path / "d", config_dir=tmp_path / "c",
        baseline_dir=tmp_path / "b", report_path=tmp_path / "r.json",
        image_version="v0",
    )
    assert rc == 0
