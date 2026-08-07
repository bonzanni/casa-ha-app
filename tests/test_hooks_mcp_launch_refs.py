"""#431 r2 (Terra): the pre-push launch-ref guard (`hooks._scan_mcp_launch_refs`)
had the SAME containment gap as `plugin_store.mcp_command_verdicts` — it tested
for the exact `${CLAUDE_PLUGIN_ROOT}/` spelling, so a defaulted reference walked
past it while still resolving outside the artifact at runtime (the CLI always
sets that variable). Fixed by sharing ONE normalizer between the two sites.

The guard had no test coverage at all, which is how the second site stayed
unfixed after the first was found. These drive the REAL function over a real
git repository, because the guard reads everything from the HEAD tree.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# Identity comes from the environment, not `git config`: a committed
# address-shaped literal is refused by the repository's own push gate, and
# these throwaway repos need no real one. No dot after "@", so it is not an
# address in any sense.
_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@localhost",
            "GIT_COMMITTER_EMAIL": "t@localhost"}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args],
                   check=True, capture_output=True, text=True, env=_GIT_ENV)


def _repo_with_mcp(tmp_path: Path, ref: str, *, create: str | None = None) -> Path:
    """A committed repo whose plugin `.mcp.json` args carry *ref*."""
    root = tmp_path / "repo"
    (root / "plug" / ".claude-plugin").mkdir(parents=True)
    (root / "plug" / ".mcp.json").write_text(json.dumps({"mcpServers": {"s": {
        "command": "python3", "args": [ref]}}}), encoding="utf-8")
    if create:
        target = root / "plug" / create
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "c")
    return root


def _scan(root: Path) -> list[str]:
    import hooks
    return hooks._scan_mcp_launch_refs(root)


def test_defaulted_plugin_root_escape_is_caught(tmp_path):
    """THE regression: the defaulted spelling must be held to the same
    containment rule as the bare one."""
    root = _repo_with_mcp(tmp_path, "${CLAUDE_PLUGIN_ROOT:-.}/../outside/x.py")
    findings = _scan(root)
    assert findings, "a defaulted-root ..-escape must be a finding"
    assert any("escapes the plugin root" in f for f in findings), findings


def test_bare_plugin_root_escape_is_still_caught(tmp_path):
    root = _repo_with_mcp(tmp_path, "${CLAUDE_PLUGIN_ROOT}/../outside/x.py")
    assert any("escapes the plugin root" in f for f in _scan(root))


def test_defaulted_plugin_root_missing_target_is_caught(tmp_path):
    """Not only traversal — a defaulted reference to a file that is not in
    the pushed commit must still be reported."""
    root = _repo_with_mcp(tmp_path, "${CLAUDE_PLUGIN_ROOT:-.}/server/gone.py")
    assert _scan(root), "a missing target behind a default must be a finding"


def test_a_contained_reference_is_clean_in_both_spellings(tmp_path):
    for i, ref in enumerate(("${CLAUDE_PLUGIN_ROOT}/server/ok.py",
                             "${CLAUDE_PLUGIN_ROOT:-.}/server/ok.py")):
        root = _repo_with_mcp(tmp_path / f"case{i}", ref,
                              create="server/ok.py")
        assert _scan(root) == [], (ref, _scan(root))
