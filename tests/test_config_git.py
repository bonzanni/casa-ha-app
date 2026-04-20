"""Tests for config_git.py — git-backed config history."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI not installed",
)


def _seed(base: Path) -> None:
    (base / "agents").mkdir(parents=True)
    (base / "policies").mkdir(parents=True)
    (base / "schema").mkdir(parents=True)
    (base / "agents" / "marker.txt").write_text("x", encoding="utf-8")


class TestInitRepo:
    def test_creates_git_dir(self, tmp_path):
        from config_git import init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        assert (tmp_path / ".git").is_dir()

    def test_is_idempotent(self, tmp_path):
        from config_git import init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        sha1 = subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        ).decode().strip()

        init_repo(str(tmp_path))  # second call
        sha2 = subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        ).decode().strip()

        assert sha1 == sha2  # no new commit on re-init

    def test_initial_commit_tracks_agents_policies_schema(self, tmp_path):
        from config_git import init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        tracked = subprocess.check_output(
            ["git", "-C", str(tmp_path), "ls-files"],
        ).decode().splitlines()
        assert "agents/marker.txt" in tracked
        assert ".gitignore" in tracked


class TestCommitConfig:
    def test_returns_sha(self, tmp_path):
        from config_git import init_repo, commit_config

        _seed(tmp_path)
        init_repo(str(tmp_path))

        (tmp_path / "agents" / "new.txt").write_text("y", encoding="utf-8")
        sha = commit_config(str(tmp_path), "add new")

        assert len(sha) == 40  # full sha

    def test_commit_with_no_changes_returns_empty(self, tmp_path):
        from config_git import init_repo, commit_config

        _seed(tmp_path)
        init_repo(str(tmp_path))
        sha = commit_config(str(tmp_path), "no-op")
        assert sha == ""


class TestSnapshotManualEdits:
    def test_records_snapshot_when_dirty(self, tmp_path):
        from config_git import init_repo, snapshot_manual_edits

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "agents" / "marker.txt").write_text("z", encoding="utf-8")
        sha = snapshot_manual_edits(str(tmp_path))
        assert sha is not None

    def test_returns_none_when_clean(self, tmp_path):
        from config_git import init_repo, snapshot_manual_edits

        _seed(tmp_path)
        init_repo(str(tmp_path))
        assert snapshot_manual_edits(str(tmp_path)) is None


class TestRestoreFile:
    def test_restores_prior_content(self, tmp_path):
        from config_git import init_repo, commit_config, restore_file

        _seed(tmp_path)
        init_repo(str(tmp_path))
        original_sha = subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        ).decode().strip()

        # Modify + commit so there's a new HEAD.
        (tmp_path / "agents" / "marker.txt").write_text(
            "modified", encoding="utf-8",
        )
        commit_config(str(tmp_path), "modify marker")

        # Restore original.
        restore_file(str(tmp_path), original_sha, "agents/marker.txt")

        # The file should again contain "x", and the restore itself is
        # committed.
        assert (tmp_path / "agents" / "marker.txt").read_text() == "x"
