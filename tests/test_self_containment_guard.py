"""self_containment_guard — pre-push grep for §2.0 anti-patterns."""
from __future__ import annotations

from pathlib import Path

import pytest

from hooks import HOOK_POLICIES

pytestmark = pytest.mark.asyncio


def _build_git_push_input(cwd: Path) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin main", "description": "push"},
        "cwd": str(cwd),
    }


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


@pytest.fixture
def plugin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "casa-plugin-x"
    (repo / ".claude-plugin").mkdir(parents=True)
    _write(repo / ".claude-plugin" / "plugin.json",
           '{"name":"x","description":"d","version":"0.1.0","author":"a"}')
    _write(repo / "README.md", "# x\nNormal readme.\n")
    return repo


async def _run_policy(input_data: dict) -> dict:
    factory = HOOK_POLICIES["self_containment_guard"]["factory"]
    hook = factory()
    return await hook(input_data, None, {})


class TestSelfContainmentGuard:
    async def test_clean_repo_allows(self, plugin_repo: Path) -> None:
        result = await _run_policy(_build_git_push_input(plugin_repo))
        assert result == {}, f"unexpected deny: {result}"

    async def test_readme_please_install_blocks(self, plugin_repo: Path) -> None:
        _write(plugin_repo / "README.md", "# x\nplease install ffmpeg manually.\n")
        result = await _run_policy(_build_git_push_input(plugin_repo))
        assert result is not None
        assert "self_containment_guard" in result["hookSpecificOutput"]["permissionDecisionReason"].lower()

    async def test_apt_install_in_script_blocks(self, plugin_repo: Path) -> None:
        _write(plugin_repo / "scripts" / "setup.sh",
               "#!/bin/sh\napt install -y ffmpeg\n")
        result = await _run_policy(_build_git_push_input(plugin_repo))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_hardcoded_nonbaseline_path_blocks(self, plugin_repo: Path) -> None:
        _write(plugin_repo / "server.py",
               "import subprocess\nsubprocess.run(['/usr/bin/terraform', 'plan'])\n")
        result = await _run_policy(_build_git_push_input(plugin_repo))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_non_push_bash_allowed(self, plugin_repo: Path) -> None:
        input_data = {
            "tool_name": "Bash",
            "tool_input": {"command": "git status", "description": ""},
            "cwd": str(plugin_repo),
        }
        result = await _run_policy(input_data)
        assert result == {}

    async def test_registered_in_hook_policies(self) -> None:
        assert "self_containment_guard" in HOOK_POLICIES
        assert HOOK_POLICIES["self_containment_guard"]["matcher"] == "Bash"


# ---------------------------------------------------------------------------
# P2 (2026-07-18 self-containment plan): untracked/ignored MCP launch refs
# + the real, auditable override.
# ---------------------------------------------------------------------------

import json
import subprocess


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(repo), "PATH": "/usr/bin:/bin"})


@pytest.fixture
def git_plugin_repo(plugin_repo: Path) -> Path:
    _git(plugin_repo, "init", "-q")
    _git(plugin_repo, "add", "-A")
    _git(plugin_repo, "commit", "-qm", "init")
    return plugin_repo


def _mcp(repo: Path, servers: dict, commit: bool = True) -> None:
    _write(repo / ".mcp.json", json.dumps({"mcpServers": servers}))
    if commit:
        _git(repo, "add", ".mcp.json")
        _git(repo, "commit", "-qm", "mcp")


def _deny_reason(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


class TestMcpLaunchRefTracking:
    async def test_gitignored_venv_ref_blocks(self, git_plugin_repo: Path):
        """The gmail-v0.2.0 repro: the interpreter EXISTS in the working tree
        but is gitignored — the installed artifact will not contain it."""
        repo = git_plugin_repo
        _write(repo / ".gitignore", "server/.venv/\n")
        _write(repo / "server" / ".venv" / "bin" / "python", "fake")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore")
        _mcp(repo, {"gmail": {
            "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}})
        result = await _run_policy(_build_git_push_input(repo))
        assert result and result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert ".venv" in _deny_reason(result)
        assert "not in the pushed commit" in _deny_reason(result)

    async def test_tracked_refs_allow(self, git_plugin_repo: Path):
        repo = git_plugin_repo
        _write(repo / "server" / "server.py", "print('serve')\n")
        _git(repo, "add", "server/server.py")
        _git(repo, "commit", "-qm", "server")
        _mcp(repo, {"s": {"command": "python3",
                          "args": ["${CLAUDE_PLUGIN_ROOT}/server/server.py"]}})
        result = await _run_policy(_build_git_push_input(repo))
        assert result == {}, f"unexpected deny: {result}"

    async def test_parent_escape_ref_blocks(self, git_plugin_repo: Path):
        repo = git_plugin_repo
        _mcp(repo, {"s": {"command": "python3",
                          "args": ["${CLAUDE_PLUGIN_ROOT}/../outside.py"]}})
        result = await _run_policy(_build_git_push_input(repo))
        assert result and result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "escapes" in _deny_reason(result)

    async def test_absolute_after_interpolation_blocks(self, git_plugin_repo: Path):
        repo = git_plugin_repo
        _mcp(repo, {"s": {"command": "${CLAUDE_PLUGIN_ROOT}//etc/ssl/x"}})
        result = await _run_policy(_build_git_push_input(repo))
        assert result and result["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_resolves_repo_root_from_subdir_cwd(self, git_plugin_repo: Path):
        """Never assume tool cwd == repo root: push may run from a subdir."""
        repo = git_plugin_repo
        _write(repo / ".gitignore", "server/.venv/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore")
        _mcp(repo, {"s": {
            "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}})
        sub = repo / "server"
        sub.mkdir(exist_ok=True)
        result = await _run_policy(_build_git_push_input(sub))
        assert result and result["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_non_git_cwd_skips_tracking_check(self, plugin_repo: Path):
        """No repo → tracked-ness is unjudgeable; other checks still apply."""
        _write(plugin_repo / ".mcp.json", json.dumps({"mcpServers": {
            "s": {"command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}}}))
        result = await _run_policy(_build_git_push_input(plugin_repo))
        assert result == {}

    async def test_override_env_prefix_allows_and_logs(
            self, git_plugin_repo: Path, caplog):
        repo = git_plugin_repo
        _write(repo / ".gitignore", "server/.venv/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore")
        _mcp(repo, {"s": {
            "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}})
        input_data = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "CASA_ALLOW_ANTI_PATTERN=1 git push origin main",
                "description": "push"},
            "cwd": str(repo),
        }
        import logging
        with caplog.at_level(logging.WARNING):
            result = await _run_policy(input_data)
        assert result == {}
        assert any("override" in r.message and ".venv" in r.message
                   for r in caplog.records)

    async def test_denial_text_advertises_real_override(self, git_plugin_repo: Path):
        repo = git_plugin_repo
        _mcp(repo, {"s": {"command": "${CLAUDE_PLUGIN_ROOT}/gone/bin/x"}})
        result = await _run_policy(_build_git_push_input(repo))
        reason = _deny_reason(result)
        assert "CASA_ALLOW_ANTI_PATTERN=1" in reason
        assert "--allow-anti-pattern" not in reason


class TestGuardArmingAndOracle:
    """Sol r4-3/4/6/7: recognizer variants, HEAD-tree oracle, env refs,
    subdir cwd for the original tree scan."""

    def _push(self, repo: Path, cmd: str) -> dict:
        return {"tool_name": "Bash",
                "tool_input": {"command": cmd, "description": ""},
                "cwd": str(repo)}

    async def _denied(self, repo: Path, cmd: str) -> bool:
        r = await _run_policy(self._push(repo, cmd))
        return bool(r) and r["hookSpecificOutput"]["permissionDecision"] == "deny"

    @pytest.fixture
    def bad_repo(self, git_plugin_repo: Path) -> Path:
        repo = git_plugin_repo
        _write(repo / ".gitignore", "server/.venv/\n")
        # Mirror the real incident: the dev-only venv EXISTS in the worktree.
        _write(repo / "server" / ".venv" / "bin" / "python", "fake")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore")
        _mcp(repo, {"s": {
            "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}})
        return repo

    async def test_foreign_env_prefix_still_scans(self, bad_repo: Path):
        assert await self._denied(bad_repo, "FOO=1 git push origin main")

    async def test_git_dash_c_push_scans(self, bad_repo: Path):
        assert await self._denied(bad_repo, "git -C . push origin main")

    async def test_compound_command_push_scans(self, bad_repo: Path):
        assert await self._denied(
            bad_repo, "cd server && git push origin main")

    async def test_git_stash_push_does_not_arm(self, git_plugin_repo: Path):
        r = await _run_policy(self._push(git_plugin_repo,
                                         "git stash push -m wip"))
        assert r == {}

    async def test_quoted_override_allows_and_logs(self, bad_repo: Path, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            r = await _run_policy(self._push(
                bad_repo, "CASA_ALLOW_ANTI_PATTERN='1' git push origin main"))
        assert r == {}
        assert any("override" in rec.message for rec in caplog.records)

    async def test_staged_but_uncommitted_ref_blocks(self, git_plugin_repo: Path):
        """Sol r4-4: ls-files sees the index; the PUSHED commit (HEAD) does
        not contain a merely-staged file — must deny."""
        repo = git_plugin_repo
        _mcp(repo, {"s": {"command": "python3",
                          "args": ["${CLAUDE_PLUGIN_ROOT}/server/server.py"]}})
        _write(repo / "server" / "server.py", "print('x')\n")
        _git(repo, "add", "server/server.py")   # staged, NOT committed
        assert await self._denied(repo, "git push origin main")

    async def test_untracked_env_vendor_dir_blocks(self, git_plugin_repo: Path):
        """Sol r4-6: PYTHONPATH vendor dir gitignored → deny."""
        repo = git_plugin_repo
        _write(repo / ".gitignore", "server/vendor/\n")
        _write(repo / "server" / "vendor" / "pkg" / "__init__.py", "")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-qm", "ignore")
        _mcp(repo, {"s": {"command": "python3",
                          "env": {"PYTHONPATH":
                                  "${CLAUDE_PLUGIN_ROOT}/server/vendor"}}})
        assert await self._denied(repo, "git push origin main")

    async def test_subdir_cwd_scans_whole_tree_for_anti_patterns(
            self, git_plugin_repo: Path):
        """Sol r4-7: from repo/server, a root README anti-pattern must still
        be found (tree scan runs from the repo root, not cwd)."""
        repo = git_plugin_repo
        _write(repo / "README.md", "# x\nplease install ffmpeg manually.\n")
        sub = repo / "server"
        sub.mkdir(exist_ok=True)
        assert await self._denied(sub, "git push origin main")

    async def test_mcp_json_read_from_head_not_worktree(self, bad_repo: Path):
        """Sol r5-2: the PUSHED commit's .mcp.json is what ships — deleting
        or fixing it only in the worktree must not hide the broken commit."""
        (bad_repo / ".mcp.json").unlink()
        assert await self._denied(bad_repo, "git push origin main")

    async def test_worktree_only_breakage_does_not_block(
            self, git_plugin_repo: Path):
        """Converse of r5-2: HEAD is clean; an uncommitted broken .mcp.json
        is not in the pushed commit and must not deny."""
        repo = git_plugin_repo
        _write(repo / "server" / "server.py", "print('x')\n")
        _git(repo, "add", "server/server.py")
        _git(repo, "commit", "-qm", "server")
        _mcp(repo, {"s": {"command": "python3",
                          "args": ["${CLAUDE_PLUGIN_ROOT}/server/server.py"]}})
        _write(repo / ".mcp.json", json.dumps({"mcpServers": {"s": {
            "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}}}))
        r = await _run_policy(self._push(repo, "git push origin main"))
        assert r == {}, f"unexpected deny: {r}"

    async def test_git_dash_c_other_repo_scans_target(
            self, tmp_path: Path, bad_repo: Path):
        """Sol r5-3: `git -C <other> push` must scan the TARGET repo."""
        clean = tmp_path / "clean-cwd"
        clean.mkdir()
        assert await self._denied(
            clean, f"git -C {bad_repo} push origin main")

    async def test_cd_other_repo_scans_target(
            self, tmp_path: Path, bad_repo: Path):
        clean = tmp_path / "clean-cwd2"
        clean.mkdir()
        assert await self._denied(
            clean, f"cd {bad_repo} && git push origin main")

    async def test_reserved_env_self_declaration_blocks(
            self, git_plugin_repo: Path):
        """G6 corrected: a committed .mcp.json self-declaring a CLI-reserved
        env var must block at push time (it shadows the CLI's native value
        with a literal at runtime)."""
        repo = git_plugin_repo
        _write(repo / "server.py", "print('x')\n")
        _git(repo, "add", "server.py")
        _git(repo, "commit", "-qm", "server")
        _mcp(repo, {"s": {
            "command": "python3",
            "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"],
            "env": {"CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}"}}})
        r = await _run_policy(self._push(repo, "git push origin main"))
        assert r and r["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "CLI-reserved" in _deny_reason(r)

    async def test_reserved_env_worktree_only_does_not_block(
            self, git_plugin_repo: Path):
        """HEAD-tree semantics: an uncommitted reserved-key declaration is
        not in the pushed commit — must not deny."""
        repo = git_plugin_repo
        _write(repo / "server.py", "print('x')\n")
        _git(repo, "add", "server.py")
        _git(repo, "commit", "-qm", "server")
        _mcp(repo, {"s": {"command": "python3",
                          "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"]}})
        _write(repo / ".mcp.json", json.dumps({"mcpServers": {"s": {
            "command": "python3",
            "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"],
            "env": {"CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}"}}}}))
        r = await _run_policy(self._push(repo, "git push origin main"))
        assert r == {}, f"unexpected deny: {r}"

    async def test_git_dash_c_quoted_spaced_path_scans_target(
            self, tmp_path: Path, git_plugin_repo: Path):
        """Sol r6-1: a quoted -C path containing spaces must still arm."""
        import shutil
        spaced = tmp_path / "bad repo"
        shutil.copytree(git_plugin_repo, spaced)
        _mcp(spaced, {"s": {
            "command": "${CLAUDE_PLUGIN_ROOT}/server/.venv/bin/python"}})
        clean = tmp_path / "clean-cwd3"
        clean.mkdir()
        assert await self._denied(
            clean, f'git -C "{spaced}" push origin main')
