"""H16/H13 — plugin/marketplace/1Password tool handlers must not run their
blocking subprocess chains on the shared event loop.

Each handler (install/uninstall/marketplace_*, list_vault_items,
get_item_fields) used to call sync ``subprocess.run`` inline; on casa-main's
single asyncio loop that froze Telegram, voice, health probes and all bus
dispatch for the full subprocess runtime (up to 300s per plugin install).
These tests pin that the handlers now offload via ``asyncio.to_thread`` by
proving a heartbeat coroutine keeps ticking while a slow subprocess runs.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def user_mkt(tmp_path: Path, monkeypatch) -> Path:
    """User marketplace with one plugin that has no systemRequirements."""
    target = tmp_path / "marketplace" / ".claude-plugin" / "marketplace.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "name": "casa-plugins", "owner": {"name": "t"},
        "plugins": [
            {"name": "demo", "description": "x", "version": "0.1.0",
             "source": {"source": "github", "repo": "u/demo", "sha": "abc"},
             "category": "productivity"},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr("marketplace_ops.USER_MARKETPLACE_PATH", target)
    monkeypatch.setattr("tools._AGENT_HOME_ROOT", tmp_path / "agent-home")
    return target


def _slow_run(*_a, **_k):
    time.sleep(0.3)  # simulate `claude plugin install` / `op` latency
    m = mock.Mock()
    m.returncode = 0
    m.stdout = "[]"
    m.stderr = ""
    return m


async def _assert_loop_stays_live(coro):
    ticks: list[float] = []

    async def heartbeat():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)  # let heartbeat start
    try:
        await coro
    finally:
        hb.cancel()
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    worst = max(gaps) if gaps else float("inf")
    assert gaps and worst < 0.15, (
        f"event loop stalled: {len(ticks)} ticks, max heartbeat gap "
        f"{worst:.3f}s (a sync subprocess ran on the loop instead of "
        "asyncio.to_thread)"
    )


@patch("tools.subprocess.run", side_effect=_slow_run)
async def test_install_casa_plugin_does_not_stall_event_loop(_mock_run, user_mkt):
    from tools import install_casa_plugin
    await _assert_loop_stays_live(
        install_casa_plugin.handler({"plugin_name": "demo", "targets": ["assistant"]})
    )


@patch("tools.subprocess.run", side_effect=_slow_run)
async def test_list_vault_items_does_not_stall_event_loop(_mock_run):
    from tools import list_vault_items
    await _assert_loop_stays_live(
        list_vault_items.handler({"query": "demo", "vault": ""})
    )


@patch("tools.subprocess.run", side_effect=_slow_run)
async def test_marketplace_update_plugin_does_not_stall_event_loop(_mock_run, user_mkt):
    from tools import marketplace_update_plugin
    await _assert_loop_stays_live(
        marketplace_update_plugin.handler({"plugin_name": "demo", "new_ref": "def456"})
    )
