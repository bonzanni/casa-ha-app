"""Verify <delegates> block reaches the SDK system prompt at turn time."""

from __future__ import annotations

import pytest

from agent_registry import AgentRegistry
from config import AgentConfig, CharacterConfig, DelegateEntry, ToolsConfig

pytestmark = pytest.mark.asyncio


def _cfg(role: str, name: str, *, delegates=None) -> AgentConfig:
    return AgentConfig(
        role=role,
        model="x",
        character=CharacterConfig(name=name),
        tools=ToolsConfig(allowed=[], permission_mode="acceptEdits"),
        system_prompt="base prompt",
        delegates=list(delegates or []),
    )


def test_delegates_block_renders_with_name_and_role():
    assistant_cfg = _cfg(
        "assistant", "Ellen",
        delegates=[
            DelegateEntry(
                agent="butler", purpose="Device control.",
                when="User asks to turn things on/off.",
            ),
            DelegateEntry(
                agent="finance", purpose="Money.",
                when="User asks about money.",
            ),
        ],
    )
    butler_cfg = _cfg("butler", "Tina")
    finance_cfg = _cfg("finance", "Alex")

    reg = AgentRegistry.build(
        residents={"assistant": assistant_cfg, "butler": butler_cfg},
        specialists={"finance": finance_cfg},
    )

    from agent import _render_delegates_block
    block = _render_delegates_block(assistant_cfg.delegates, reg)
    assert "<delegates>" in block
    assert "</delegates>" in block
    assert "Tina (role: butler)" in block
    assert "Alex (role: finance)" in block
    assert "Device control." in block


def test_delegates_block_omitted_when_no_delegates():
    cfg = _cfg("butler", "Tina")  # no delegates
    reg = AgentRegistry.build(residents={"butler": cfg}, specialists={})
    from agent import _render_delegates_block
    assert _render_delegates_block(cfg.delegates, reg) == ""


def test_executors_block_renders_for_assistant():
    from agent import _render_executors_block
    from config import ExecutorEntry
    cfg = _cfg("assistant", "Ellen")
    cfg.executors = [
        ExecutorEntry(
            executor_type="configurator",
            purpose="Edit configs.",
            when="User wants to change configuration.",
        ),
    ]
    block = _render_executors_block(cfg.executors)
    assert "<executors>" in block
    assert "configurator" in block
    assert "Edit configs." in block
    assert "Engage when: User wants to change configuration." in block


def test_executors_block_omitted_when_empty():
    from agent import _render_executors_block
    assert _render_executors_block([]) == ""
