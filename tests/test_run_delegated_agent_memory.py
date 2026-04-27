"""Specialist memory read+write inside _run_delegated_agent (M4b).

Tests are TDD-shaped: each one fails on master tip 93b442e because
_run_delegated_agent at tools.py:318 does not yet call ensure_session,
get_context, or add_turn.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tools
from config import (
    AgentConfig, CharacterConfig, MemoryConfig, SessionConfig, ToolsConfig,
)


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# _FakeSpecialistClient — minimal ClaudeSDKClient stand-in
# ---------------------------------------------------------------------------


class _FakeSpecialistClient:
    """Captures the prompt passed via .query() and yields a configurable reply."""

    captured_prompt: str = ""
    response_text: str = "finance reply"

    @classmethod
    def reset(cls, response: str = "finance reply") -> None:
        cls.captured_prompt = ""
        cls.response_text = response

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def query(self, text: str) -> None:
        type(self).captured_prompt = text

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, TextBlock
        # Defensive shape construction (SDK fields drift across versions).
        try:
            block = TextBlock(text=type(self).response_text)
        except TypeError:
            block = TextBlock(type(self).response_text)  # type: ignore[call-arg]
        try:
            asst = AssistantMessage(content=[block])
        except TypeError:
            asst = AssistantMessage.__new__(AssistantMessage)
            asst.content = [block]  # type: ignore[attr-defined]
        yield asst


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _specialist_cfg(role: str = "finance", token_budget: int = 0) -> AgentConfig:
    """Minimal specialist AgentConfig.

    token_budget == 0 → stateless (today's behavior; M4b back-compat).
    token_budget > 0 → memory-bearing (M4b opt-in).
    """
    return AgentConfig(
        role=role,
        model="claude-sonnet-4-6",
        system_prompt=f"You are {role}",
        character=CharacterConfig(name=role.capitalize()),
        enabled=True,
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=token_budget),
        session=SessionConfig(strategy="ephemeral", idle_timeout=0),
    )


def _make_memory_provider(*, get_context_returns: str = "") -> MagicMock:
    """Return an AsyncMock-decorated provider that records all calls."""
    mp = MagicMock()
    mp.ensure_session = AsyncMock(return_value=None)
    mp.get_context = AsyncMock(return_value=get_context_returns)
    mp.add_turn = AsyncMock(return_value=None)
    return mp


def _patch_active_memory_provider(monkeypatch, mp: MagicMock | None) -> None:
    """Set agent_mod.active_memory_provider for the test scope.

    tools.py reads `getattr(agent_mod, "active_memory_provider", None)`
    at call time, so monkey-patching the agent module is sufficient.
    """
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_memory_provider", mp, raising=False)


def _set_origin(monkeypatch, *, role: str = "assistant",
                channel: str = "telegram", chat_id: str = "abc",
                scope: str = "personal") -> None:
    """Stamp origin_var so _run_delegated_agent sees a parent context."""
    import agent as agent_mod
    agent_mod.origin_var.set({
        "role": role,
        "channel": channel,
        "chat_id": chat_id,
        "scope": scope,
        "delegation_depth": 0,
    })
