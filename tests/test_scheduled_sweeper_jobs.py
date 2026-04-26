"""Regression tests for v0.15.2: AsyncIOScheduler sweeper wiring.

casa_core.py registers two scheduled jobs with the AsyncIOScheduler —
`engagement_idle_sweep` (cron 08:00 daily) and `workspace_sweep`
(interval 6h). Until v0.15.2 these were registered as
``lambda: asyncio.create_task(coro(...))``. APScheduler's
``AsyncIOExecutor`` runs sync callables in a worker thread, so the
lambda fired in a thread with no running loop and raised
``RuntimeError: no running event loop`` on every fire — silent no-op
since v0.13.0.

The fix: pass the coroutine function directly (with ``kwargs={...}``);
``AsyncIOExecutor`` dispatches coroutines on the running loop natively.
This file pins the contract so the regression can't sneak back.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path


def test_engagement_idle_sweep_target_is_coroutine_function():
    from engagement_registry import EngagementRegistry

    assert asyncio.iscoroutinefunction(EngagementRegistry.sweep_idle_and_suspend), (
        "EngagementRegistry.sweep_idle_and_suspend must be a coroutine "
        "function so AsyncIOScheduler dispatches it on the running loop. "
        "If this fails after a refactor, do NOT re-wrap with a sync "
        "lambda — find another fix."
    )


def test_workspace_sweep_target_is_coroutine_function():
    from drivers.workspace import _sweep_workspaces

    assert asyncio.iscoroutinefunction(_sweep_workspaces), (
        "_sweep_workspaces must be a coroutine function so "
        "AsyncIOScheduler dispatches it on the running loop. If this "
        "fails after a refactor, do NOT re-wrap with a sync lambda — "
        "find another fix."
    )


def test_casa_core_does_not_register_sync_lambda_sweepers():
    """Static guard: casa_core.py must not register sweeper jobs as
    ``lambda: asyncio.create_task(...)``. That pattern caused
    ``RuntimeError: no running event loop`` on every fire from v0.13.0
    through v0.15.1."""
    casa_core_src = (
        Path(__file__).resolve().parent.parent
        / "casa-agent"
        / "rootfs"
        / "opt"
        / "casa"
        / "casa_core.py"
    )
    text = casa_core_src.read_text(encoding="utf-8")
    assert not re.search(r"lambda:\s*asyncio\.create_task\b", text), (
        "Found `lambda: asyncio.create_task(...)` in casa_core.py — this "
        "re-introduces the v0.13.0 sweeper bug. Pass the coroutine "
        "function directly to scheduler.add_job(...) with kwargs={...} "
        "instead."
    )
