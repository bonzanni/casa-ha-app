"""M28 — self_containment_guard's tree scan must run off the event loop and
must not read files whose names match none of the checks.

The pre-push guard used to ``os.walk`` the whole cwd and ``read_text`` EVERY
file (reading arbitrarily large/binary files) synchronously on the shared
event loop before applying the filename filters. These tests pin the fixed
behavior: a ``_scan_tree_for_anti_patterns`` helper that filters by filename
BEFORE opening, caps the read, and is dispatched via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import builtins
import time
from pathlib import Path

import pytest

from hooks import HOOK_POLICIES

pytestmark = pytest.mark.unit


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


def _build_git_push_input(cwd: Path) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin main", "description": "push"},
        "cwd": str(cwd),
    }


async def _run_policy(input_data: dict) -> dict:
    hook = HOOK_POLICIES["self_containment_guard"]["factory"]()
    return await hook(input_data, None, {})


def test_non_matching_files_are_never_opened(plugin_repo, monkeypatch):
    # A binary-named file whose CONTENT would match a check must not be opened.
    _write(plugin_repo / "dist" / "asset.bin", "apt install ffmpeg\n" * 1000)
    opened: list[str] = []
    real_open = builtins.open

    def spy_open(file, *a, **kw):
        opened.append(str(file))
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", spy_open)
    from hooks import _scan_tree_for_anti_patterns
    findings = _scan_tree_for_anti_patterns(plugin_repo)
    assert findings == []
    assert not any(name.endswith("asset.bin") for name in opened), \
        f"non-matching file was opened: {opened}"


def test_read_capped_at_max_scan_bytes(plugin_repo):
    # A pattern placed beyond the cap is not flagged (documents cap semantics).
    from hooks import _MAX_SCAN_BYTES, _scan_tree_for_anti_patterns
    _write(plugin_repo / "scripts" / "big.sh",
           "# pad\n" + ("x" * (_MAX_SCAN_BYTES + 10)) + "\napt install ffmpeg\n")
    assert _scan_tree_for_anti_patterns(plugin_repo) == []


def test_matching_anti_patterns_still_flagged(plugin_repo):
    from hooks import _scan_tree_for_anti_patterns
    _write(plugin_repo / "scripts" / "setup.sh", "#!/bin/sh\napt install -y ffmpeg\n")
    _write(plugin_repo / "README.md", "# x\nplease install ffmpeg manually.\n")
    findings = _scan_tree_for_anti_patterns(plugin_repo)
    assert any("apt/yum install" in f for f in findings)
    assert any("please install" in f.lower() for f in findings)


@pytest.mark.asyncio
async def test_scan_runs_off_loop_and_denies(plugin_repo, monkeypatch):
    """A slow tree scan must not freeze the event loop, and the guard must
    still deny when the (slow) scan reports findings."""
    import hooks

    def slow_scan(cwd):
        time.sleep(0.3)
        return [f"{cwd}/scripts/setup.sh: apt/yum install"]

    monkeypatch.setattr(hooks, "_scan_tree_for_anti_patterns", slow_scan)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    result = await _run_policy(_build_git_push_input(plugin_repo))
    t.cancel()

    assert ticks >= 10, f"event loop starved during tree scan (ticks={ticks})"
    assert "self_containment_guard" in \
        result["hookSpecificOutput"]["permissionDecisionReason"].lower()


@pytest.mark.asyncio
async def test_clean_repo_still_allows(plugin_repo):
    assert await _run_policy(_build_git_push_input(plugin_repo)) == {}
