"""H2/M20 — resident SDK-plugin resolution must run off the event loop.

``Agent._process`` used to call ``build_sdk_plugins`` (a blocking
``claude plugin list --json`` subprocess) synchronously on every turn,
freezing the single shared event loop. These tests pin the fixed
contract: the shell-out is offloaded via ``asyncio.to_thread`` AND cached
per Agent instance (the install doctrine makes ``casa_reload(scope=agent)``
mandatory after a plugin change, which constructs a fresh Agent).
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import agent as agent_mod
from agent import Agent
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from session_registry import SessionRegistry

pytestmark = pytest.mark.unit


def _make_agent(tmp_path, role: str = "assistant") -> Agent:
    cfg = AgentConfig(
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
    )
    return Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )


def test_sdk_plugins_resolved_off_loop_and_cached(tmp_path, monkeypatch):
    calls: list[threading.Thread] = []

    def fake_build(**kw):
        calls.append(threading.current_thread())
        return [{"type": "local", "path": "/x"}]

    monkeypatch.setattr(agent_mod, "build_sdk_plugins", fake_build)
    a = _make_agent(tmp_path)

    async def run():
        loop_thread = threading.current_thread()
        p1 = await a._get_sdk_plugins()
        p2 = await a._get_sdk_plugins()
        assert p1 == p2 == [{"type": "local", "path": "/x"}]
        # Second turn served from cache — builder ran exactly once.
        assert len(calls) == 1
        # Executed via asyncio.to_thread, not on the loop thread.
        assert calls[0] is not loop_thread

    asyncio.run(run())


def test_degraded_empty_result_is_retried_not_pinned(tmp_path, monkeypatch):
    results: list[list] = [[], [{"type": "local", "path": "/x"}]]

    def fake_build(**kw):
        return results.pop(0)

    monkeypatch.setattr(agent_mod, "build_sdk_plugins", fake_build)
    a = _make_agent(tmp_path)

    async def run():
        first = await a._get_sdk_plugins()
        second = await a._get_sdk_plugins()
        assert first == []
        assert second == [{"type": "local", "path": "/x"}]
        assert results == []  # fake called exactly twice

    asyncio.run(run())


def test_get_sdk_plugins_does_not_freeze_loop(tmp_path, monkeypatch):
    def slow_build(**kw):
        time.sleep(0.3)
        return [{"type": "local", "path": "/x"}]

    monkeypatch.setattr(agent_mod, "build_sdk_plugins", slow_build)
    a = _make_agent(tmp_path)

    async def run():
        ticks = 0

        async def tick():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        t = asyncio.create_task(tick())
        await asyncio.sleep(0)  # let the ticker start
        await a._get_sdk_plugins()
        t.cancel()
        # Pre-fix (synchronous call) the loop is frozen for the whole 0.3s
        # and ticks stays ~0; post-fix the ticker keeps running.
        assert ticks >= 10, f"event loop starved during plugin resolve (ticks={ticks})"

    asyncio.run(run())
