"""Verify <delegation_context> block is prepended to delegated calls."""

from __future__ import annotations

import pytest

import tools
import agent as agent_mod
from agent_registry import AgentRegistry
from config import AgentConfig, CharacterConfig

pytestmark = pytest.mark.asyncio


def _make_cfg(role: str, name: str) -> AgentConfig:
    return AgentConfig(
        role=role, model="x",
        character=CharacterConfig(name=name),
        system_prompt="x",
    )


async def test_delegation_context_block_includes_caller_and_register(monkeypatch):
    """Origin: assistant on telegram → suggested_register=text, caller=Ellen."""
    captured_prompts: list[str] = []

    async def _fake_query(self, prompt):
        captured_prompts.append(prompt)

    class _FakeClient:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        query = _fake_query
        async def receive_response(self):
            if False:
                yield None
            return

    reg = AgentRegistry.build(
        residents={
            "assistant": _make_cfg("assistant", "Ellen"),
            "butler": _make_cfg("butler", "Tina"),
        },
        specialists={},
    )
    monkeypatch.setattr(tools, "_agent_registry", reg, raising=False)
    monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeClient)

    target_cfg = _make_cfg("butler", "Tina")
    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": "1",
        "user_id": 1, "cid": "abc", "user_text": "x",
        "delegation_depth": 0,
    })
    try:
        await tools._run_delegated_agent(
            target_cfg, "turn off the lights", "(none)",
        )
    finally:
        agent_mod.origin_var.reset(token)

    assert captured_prompts, "expected at least one query"
    prompt = captured_prompts[0]
    assert "<delegation_context>" in prompt
    assert "caller_role: assistant" in prompt
    assert "caller_name: Ellen" in prompt
    assert "originating_channel: telegram" in prompt
    assert "suggested_register: text" in prompt
    assert "Task: turn off the lights" in prompt


async def test_delegation_context_voice_channel_yields_voice_register(monkeypatch):
    captured: list[str] = []

    async def _fake_query(self, prompt):
        captured.append(prompt)

    class _FakeClient:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        query = _fake_query
        async def receive_response(self):
            if False:
                yield None
            return

    reg = AgentRegistry.build(
        residents={"butler": _make_cfg("butler", "Tina")},
        specialists={},
    )
    monkeypatch.setattr(tools, "_agent_registry", reg, raising=False)
    monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeClient)

    target_cfg = _make_cfg("butler", "Tina")
    token = agent_mod.origin_var.set({
        "role": "butler", "channel": "voice", "chat_id": "1",
        "user_id": 1, "cid": "abc", "user_text": "x",
        "delegation_depth": 0,
    })
    try:
        await tools._run_delegated_agent(target_cfg, "x", "")
    finally:
        agent_mod.origin_var.reset(token)

    assert "suggested_register: voice" in captured[0]
