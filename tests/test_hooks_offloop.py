"""M17 — commit_size_guard must not run `git status` on the event loop.

The guard fires on every Write/Edit; it used to call the synchronous
``_git_porcelain_count`` (a ``git status --porcelain`` subprocess, up to a
5s timeout) inline, freezing the single shared event loop for the whole
git invocation. This pins that the git call is now offloaded via
``asyncio.to_thread`` so concurrent channels keep running.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_commit_size_guard_does_not_block_event_loop(monkeypatch):
    import hooks

    def slow_git_count(*_a, **_k) -> int:
        time.sleep(0.3)  # simulate a slow `git status --porcelain`
        return 0

    monkeypatch.setattr(hooks, "_git_porcelain_count", slow_git_count)
    hook = hooks.make_commit_size_guard_hook(max_files=20)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)  # let the ticker start before the hook runs
    out = await hook(
        {"tool_name": "Write", "tool_input": {"file_path": "/config/x"}},
        None, {},
    )
    t.cancel()

    assert out == {}
    # Pre-fix, the synchronous subprocess call freezes the loop for the whole
    # 0.3s and the ticker stays ~0; post-fix (asyncio.to_thread) it keeps
    # ticking while git runs in a worker thread.
    assert ticks >= 10, f"event loop starved during git status (ticks={ticks})"


async def test_commit_size_guard_still_denies_above_threshold(monkeypatch):
    """Offloading must not change the guard's decision semantics."""
    import hooks

    monkeypatch.setattr(hooks, "_git_porcelain_count", lambda *a, **k: 25)
    hook = hooks.make_commit_size_guard_hook(max_files=20)
    out = await hook(
        {"tool_name": "Write", "tool_input": {"file_path": "/config/x"}},
        None, {},
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
