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


class TestChangedPaths:
    """#231/#222: the reload guard uses changed_paths to tell a plugin-registry
    persist commit (already activated in-process) from a commit that also edits
    agents/ or policies/ and therefore genuinely owes a reload."""

    def test_lists_paths_touched_by_a_commit(self, tmp_path):
        from config_git import init_repo, commit_config, changed_paths

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "plugins").mkdir(parents=True, exist_ok=True)
        (tmp_path / "plugins" / "registry.json").write_text("{}", encoding="utf-8")
        (tmp_path / "agents" / "new.txt").write_text("y", encoding="utf-8")
        sha = commit_config(str(tmp_path), "mixed commit")

        paths = changed_paths(str(tmp_path), sha)
        assert "plugins/registry.json" in paths
        assert "agents/new.txt" in paths

    def test_plugins_only_commit(self, tmp_path):
        from config_git import init_repo, commit_config, changed_paths

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "plugins").mkdir(parents=True, exist_ok=True)
        (tmp_path / "plugins" / "registry.json").write_text("{}", encoding="utf-8")
        sha = commit_config(str(tmp_path), "persist plugin")

        paths = changed_paths(str(tmp_path), sha)
        assert paths == ["plugins/registry.json"]
        assert all(p.startswith("plugins/") for p in paths)

    def test_bad_sha_returns_empty_failsafe(self, tmp_path):
        from config_git import init_repo, changed_paths

        _seed(tmp_path)
        init_repo(str(tmp_path))
        assert changed_paths(str(tmp_path), "deadbeef" * 5) == []


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


class TestPluginsWhitelist:
    """Unified plugin architecture (v0.71.0): the plugin registry is config
    (tracked + versioned for an audit trail); the content-addressed store +
    staging are binaries, never tracked."""

    def test_registry_json_is_tracked(self, tmp_path):
        from config_git import commit_config, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        pl = tmp_path / "plugins"
        pl.mkdir(parents=True)
        (pl / "registry.json").write_text('{"plugins": []}', encoding="utf-8")
        sha = commit_config(str(tmp_path), "plugins: registry init")
        assert sha, "registry.json write must produce a real commit"
        tracked = subprocess.check_output(
            ["git", "-C", str(tmp_path), "ls-files"],
        ).decode().splitlines()
        assert "plugins/registry.json" in tracked

    def test_plugin_env_conf_stays_untracked(self, tmp_path):
        from config_git import commit_config, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "plugin-env.conf").write_text(
            "SECRET=op://x/y/z\n", encoding="utf-8",
        )
        sha = commit_config(str(tmp_path), "should be a no-op")
        assert sha == ""  # mode-0600 secrets file must never enter history
        tracked = subprocess.check_output(
            ["git", "-C", str(tmp_path), "ls-files"],
        ).decode().splitlines()
        assert "plugin-env.conf" not in tracked

    def test_store_and_staging_stay_untracked(self, tmp_path):
        from config_git import commit_config, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        pl = tmp_path / "plugins"
        (pl / "store" / "superpowers" / "abc").mkdir(parents=True)
        (pl / "store" / "superpowers" / "abc" / "skill.md").write_text("x")
        (pl / ".staging" / "xyz").mkdir(parents=True)
        (pl / "registry.json").write_text("{}", encoding="utf-8")
        commit_config(str(tmp_path), "plugins: registry init")
        tracked = subprocess.check_output(
            ["git", "-C", str(tmp_path), "ls-files"],
        ).decode().splitlines()
        assert "plugins/registry.json" in tracked
        assert not any(t.startswith("plugins/store/") for t in tracked)
        assert not any(t.startswith("plugins/.staging/") for t in tracked)

    def test_init_repo_refreshes_stale_gitignore(self, tmp_path):
        """Existing deployments initialized the repo with the OLD whitelist;
        init_repo must reconcile .gitignore on boot, not only on fresh init."""
        from config_git import _GITIGNORE_CONTENT, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        old = "# old whitelist\n*\n!agents/\n!agents/**\n!.gitignore\n"
        (tmp_path / ".gitignore").write_text(old, encoding="utf-8")
        subprocess.check_call(
            ["git", "-C", str(tmp_path), "commit", "-aqm", "simulate old deploy"],
        )

        init_repo(str(tmp_path))  # boot on an existing repo
        assert (tmp_path / ".gitignore").read_text(
            encoding="utf-8") == _GITIGNORE_CONTENT
        status = subprocess.check_output(
            ["git", "-C", str(tmp_path), "status", "--porcelain"],
        ).decode().strip()
        assert status == "", "refreshed .gitignore must be committed, not left dirty"

    def test_setup_configs_heredoc_matches_python_whitelist(self):
        """Two writers own the whitelist (setup-configs.sh fresh-install
        heredoc, config_git fresh-init + boot reconcile) — they drifted once
        (P-3); this pins them together."""
        import re

        from config_git import _GITIGNORE_CONTENT

        sh = (Path(__file__).resolve().parent.parent
              / "casa-agent" / "rootfs" / "etc" / "s6-overlay" / "scripts"
              / "setup-configs.sh").read_text(encoding="utf-8")
        m = re.search(r"cat > \.gitignore <<'EOF'\n(.*?)EOF\n", sh, re.S)
        assert m, "setup-configs.sh .gitignore heredoc not found"
        assert m.group(1) == _GITIGNORE_CONTENT
