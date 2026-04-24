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


async def _run_policy(input_data: dict) -> dict | None:
    factory = HOOK_POLICIES["self_containment_guard"]["factory"]
    hook = factory()
    return await hook(input_data, None, {})


class TestSelfContainmentGuard:
    async def test_clean_repo_allows(self, plugin_repo: Path) -> None:
        result = await _run_policy(_build_git_push_input(plugin_repo))
        assert result is None, f"unexpected deny: {result}"

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
        assert result is None

    async def test_registered_in_hook_policies(self) -> None:
        assert "self_containment_guard" in HOOK_POLICIES
        assert HOOK_POLICIES["self_containment_guard"]["matcher"] == "Bash"
