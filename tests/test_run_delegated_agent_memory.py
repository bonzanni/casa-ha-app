"""Specialist memory read+write inside _run_delegated_agent (M4b).

Tests are TDD-shaped: each one fails on master tip 93b442e because
_run_delegated_agent at tools.py:400 does not yet call ensure_session,
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


async def test_token_budget_zero_skips_all_memory_calls(monkeypatch):
    """A specialist with token_budget=0 keeps today's stateless behavior:
    no ensure_session, no get_context, no add_turn."""
    cfg = _specialist_cfg(role="finance", token_budget=0)
    mp = _make_memory_provider()
    _patch_active_memory_provider(monkeypatch, mp)
    _set_origin(monkeypatch)
    _FakeSpecialistClient.reset(response="finance reply")

    with patch.object(tools, "ClaudeSDKClient", _FakeSpecialistClient):
        text = await tools._run_delegated_agent(
            cfg, task_text="how is Q1 cashflow?", context_text="",
        )

    assert text == "finance reply"
    mp.ensure_session.assert_not_awaited()
    mp.get_context.assert_not_awaited()
    mp.add_turn.assert_not_awaited()


async def test_token_budget_positive_calls_ensure_and_get_context(monkeypatch):
    """A memory-bearing specialist (token_budget>0) opens a Honcho session
    keyed `f"{role}:{user_peer}"` and fetches with search_query=task_text.

    E-H (v0.31.0): get_context call no longer passes ``user_peer`` —
    v0.26.0 / E-14 dropped it from the ABC. ``ensure_session`` legitimately
    keeps user_peer (it provisions the Honcho peer pair). This test was
    locking in the very kwarg drift that the v0.30.0 exploration session
    surfaced as E-H; the assertion has been inverted to assert
    ``user_peer`` is NOT in the get_context kwargs."""
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    mp = _make_memory_provider(get_context_returns="")
    _patch_active_memory_provider(monkeypatch, mp)
    _set_origin(monkeypatch)
    _FakeSpecialistClient.reset()

    with patch.object(tools, "ClaudeSDKClient", _FakeSpecialistClient):
        await tools._run_delegated_agent(
            cfg, task_text="how is Q1 cashflow?", context_text="",
        )

    mp.ensure_session.assert_awaited_once_with(
        session_id="finance-nicola",
        agent_role="finance",
        user_peer="nicola",
    )
    mp.get_context.assert_awaited_once()
    kwargs = mp.get_context.await_args.kwargs
    assert kwargs["session_id"] == "finance-nicola"
    assert kwargs["agent_role"] == "finance"
    assert kwargs["tokens"] == 4000
    assert kwargs["search_query"] == "how is Q1 cashflow?"
    # E-H: user_peer must NOT be passed to get_context (dropped v0.26.0).
    assert "user_peer" not in kwargs


async def test_digest_injected_as_memory_context_block(monkeypatch):
    """When get_context returns a non-empty digest, the prompt sent to the
    SDK contains a <memory_context agent="finance"> block with that digest,
    placed between <delegation_context> and `Task:`."""
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    digest = (
        "## Summary so far\n"
        "Nicola asked about Q1 dining-out spend; baseline €420.\n"
        "## Recent exchanges\n"
        "- 2026-04-25 Sun: Q1 dining-out spend: €420\n"
    )
    mp = _make_memory_provider(get_context_returns=digest)
    _patch_active_memory_provider(monkeypatch, mp)
    _set_origin(monkeypatch)
    _FakeSpecialistClient.reset()

    with patch.object(tools, "ClaudeSDKClient", _FakeSpecialistClient):
        await tools._run_delegated_agent(
            cfg,
            task_text="this week's spend?",
            context_text="",
        )

    prompt = _FakeSpecialistClient.captured_prompt
    assert '<memory_context agent="finance">' in prompt
    assert "Nicola asked about Q1 dining-out spend" in prompt
    assert "</memory_context>" in prompt
    # Ordering: delegation_context → memory_context → Task:
    delegation_idx = prompt.index("<delegation_context>")
    memory_idx = prompt.index('<memory_context agent="finance">')
    task_idx = prompt.index("Task:")
    assert delegation_idx < memory_idx < task_idx


async def test_empty_digest_omits_memory_context_block(monkeypatch):
    """When get_context returns "", no memory_context block is rendered —
    the prompt looks like today's stateless prompt."""
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    mp = _make_memory_provider(get_context_returns="")
    _patch_active_memory_provider(monkeypatch, mp)
    _set_origin(monkeypatch)
    _FakeSpecialistClient.reset()

    with patch.object(tools, "ClaudeSDKClient", _FakeSpecialistClient):
        await tools._run_delegated_agent(
            cfg, task_text="hi", context_text="",
        )

    prompt = _FakeSpecialistClient.captured_prompt
    assert "<memory_context" not in prompt
    assert "Task: hi" in prompt


async def test_get_context_raises_yields_no_memory_block(monkeypatch):
    """If get_context raises, the specialist still runs — no memory_context
    block, no exception propagated."""
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    mp = _make_memory_provider()
    mp.get_context = AsyncMock(side_effect=RuntimeError("honcho boom"))
    _patch_active_memory_provider(monkeypatch, mp)
    _set_origin(monkeypatch)
    _FakeSpecialistClient.reset()

    with patch.object(tools, "ClaudeSDKClient", _FakeSpecialistClient):
        text = await tools._run_delegated_agent(
            cfg, task_text="hi", context_text="",
        )

    assert text == "finance reply"
    prompt = _FakeSpecialistClient.captured_prompt
    assert "<memory_context" not in prompt


async def test_active_memory_provider_none_skips(monkeypatch):
    """If the global memory provider is unset (NoOp / boot-degraded), the
    specialist runs as today (no memory injection), no exception."""
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    _patch_active_memory_provider(monkeypatch, None)
    _set_origin(monkeypatch)
    _FakeSpecialistClient.reset()

    with patch.object(tools, "ClaudeSDKClient", _FakeSpecialistClient):
        text = await tools._run_delegated_agent(
            cfg, task_text="hi", context_text="",
        )

    assert text == "finance reply"
    prompt = _FakeSpecialistClient.captured_prompt
    assert "<memory_context" not in prompt


async def test_add_turn_fires_with_task_body_and_reply(monkeypatch):
    """After the SDK returns, _run_delegated_agent fires a background
    add_turn writing user_text=<task_text>, assistant_text=<reply>."""
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    mp = _make_memory_provider()
    _patch_active_memory_provider(monkeypatch, mp)
    _set_origin(monkeypatch)
    _FakeSpecialistClient.reset(response="on track; June 15 ETA")

    with patch.object(tools, "ClaudeSDKClient", _FakeSpecialistClient):
        await tools._run_delegated_agent(
            cfg, task_text="Q1 cashflow?", context_text="",
        )

    # Background tasks live in tools._specialist_bg_tasks; drain them.
    bg = getattr(tools, "_specialist_bg_tasks", set())
    if bg:
        await asyncio.gather(*list(bg), return_exceptions=True)

    # Two add_turn calls expected: specialist session + parent meta scope
    # (the latter added by Task 12). Locate the specialist-session call.
    specialist_calls = [
        c for c in mp.add_turn.await_args_list
        if c.kwargs.get("session_id") == "finance-nicola"
    ]
    assert len(specialist_calls) == 1
    kwargs = specialist_calls[0].kwargs
    assert kwargs["session_id"] == "finance-nicola"
    assert kwargs["agent_role"] == "finance"
    assert kwargs["user_peer"] == "nicola"
    assert kwargs["user_text"] == "Q1 cashflow?"
    assert kwargs["assistant_text"] == "on track; June 15 ETA"


async def test_add_turn_skipped_on_empty_reply(monkeypatch):
    """If the SDK produces no text, no add_turn fires (parity with
    Agent._process at agent.py:528 — `if response_text:` gates writes)."""
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    mp = _make_memory_provider()
    _patch_active_memory_provider(monkeypatch, mp)
    _set_origin(monkeypatch)
    _FakeSpecialistClient.reset(response="")  # empty SDK reply

    with patch.object(tools, "ClaudeSDKClient", _FakeSpecialistClient):
        await tools._run_delegated_agent(
            cfg, task_text="hi", context_text="",
        )

    bg = getattr(tools, "_specialist_bg_tasks", set())
    if bg:
        await asyncio.gather(*list(bg), return_exceptions=True)

    mp.add_turn.assert_not_awaited()


async def test_add_turn_failure_does_not_surface(monkeypatch, caplog):
    """If add_turn raises, _run_delegated_agent has already returned;
    the exception is logged WARNING, never propagated."""
    import logging
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    mp = _make_memory_provider()
    mp.add_turn = AsyncMock(side_effect=RuntimeError("honcho down"))
    _patch_active_memory_provider(monkeypatch, mp)
    _set_origin(monkeypatch)
    _FakeSpecialistClient.reset(response="ok")

    caplog.set_level(logging.WARNING, logger="tools")
    with patch.object(tools, "ClaudeSDKClient", _FakeSpecialistClient):
        text = await tools._run_delegated_agent(
            cfg, task_text="hi", context_text="",
        )

    assert text == "ok"  # caller did not see the failure

    bg = getattr(tools, "_specialist_bg_tasks", set())
    if bg:
        await asyncio.gather(*list(bg), return_exceptions=True)

    # The warning emission is implementation-flexible (logger name + level
    # is what we care about). Just confirm at least one WARNING was logged.
    assert any(
        r.levelno >= logging.WARNING for r in caplog.records
    ), "expected at least one WARNING from the failed add_turn"


async def test_delegation_writes_meta_scope_summary(monkeypatch):
    """Each delegate-to-specialist call writes one summary line to the
    parent's meta session, mirroring _finalize_engagement (tools.py:1326-1338)."""
    cfg = _specialist_cfg(role="finance", token_budget=4000)
    mp = _make_memory_provider()
    _patch_active_memory_provider(monkeypatch, mp)
    _set_origin(monkeypatch, channel="telegram", chat_id="abc",
                role="assistant", scope="personal")
    _FakeSpecialistClient.reset(response="on track; June 15 ETA")

    with patch.object(tools, "ClaudeSDKClient", _FakeSpecialistClient):
        await tools._run_delegated_agent(
            cfg, task_text="Q1 cashflow?", context_text="",
        )

    bg = getattr(tools, "_specialist_bg_tasks", set())
    if bg:
        await asyncio.gather(*list(bg), return_exceptions=True)

    # Two add_turn calls expected: one to finance-nicola (specialist),
    # one to telegram-abc-meta-assistant (parent meta).
    sessions_written = {
        c.kwargs["session_id"]
        for c in mp.add_turn.await_args_list
    }
    assert "finance-nicola" in sessions_written
    assert "telegram-abc-meta-assistant" in sessions_written
