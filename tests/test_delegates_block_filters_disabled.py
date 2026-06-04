"""A disabled/unknown specialist listed in delegates.yaml must NOT be advertised
in the <delegates> system-prompt block.

Regression for the bug where a resident's static delegates.yaml entry pointing at
a specialist set ``enabled: false`` was still rendered into Ellen's prompt — so
she would try to delegate and the tool would reject it with ``unknown_agent``.
The AgentRegistry holds exactly residents + ENABLED specialists, so "disabled" ==
absent from the registry; the render must filter on that.
"""
from __future__ import annotations

import pytest

from agent import _render_delegates_block
from agent_registry import AgentRegistry
from config import AgentConfig, CharacterConfig, DelegateEntry, ToolsConfig

pytestmark = [pytest.mark.unit]


def _cfg(role: str, name: str, *, delegates=None) -> AgentConfig:
    return AgentConfig(
        role=role,
        model="x",
        character=CharacterConfig(name=name),
        tools=ToolsConfig(allowed=[], permission_mode="acceptEdits"),
        system_prompt="base prompt",
        delegates=list(delegates or []),
    )


def _assistant_with_butler_and_finance() -> AgentConfig:
    return _cfg(
        "assistant", "Ellen",
        delegates=[
            DelegateEntry(agent="butler", purpose="Device control.",
                          when="User asks to turn things on/off."),
            DelegateEntry(agent="finance", purpose="Money matters.",
                          when="User asks about money."),
        ],
    )


def test_disabled_specialist_is_omitted_from_block():
    """finance is bundled but disabled -> absent from the registry -> not advertised."""
    assistant = _assistant_with_butler_and_finance()
    butler = _cfg("butler", "Tina")
    # finance is DISABLED: not passed as a specialist (all_configs() excludes it).
    reg = AgentRegistry.build(
        residents={"assistant": assistant, "butler": butler}, specialists={},
    )
    block = _render_delegates_block(assistant.delegates, reg)
    assert "Tina (role: butler)" in block       # enabled resident still shown
    assert "Device control." in block
    assert "finance" not in block                # disabled -> omitted
    assert "Money matters." not in block


def test_enabled_specialist_is_still_shown():
    """When finance IS enabled (present in the registry), it renders as before."""
    assistant = _assistant_with_butler_and_finance()
    butler = _cfg("butler", "Tina")
    finance = _cfg("finance", "Alex")
    reg = AgentRegistry.build(
        residents={"assistant": assistant, "butler": butler},
        specialists={"finance": finance},
    )
    block = _render_delegates_block(assistant.delegates, reg)
    assert "Tina (role: butler)" in block
    assert "Alex (role: finance)" in block
    assert "Money matters." in block


def test_block_empty_when_all_delegates_disabled():
    """If every delegate is disabled/unknown, the block is omitted entirely."""
    assistant = _cfg(
        "assistant", "Ellen",
        delegates=[DelegateEntry(agent="finance", purpose="Money.", when="x")],
    )
    reg = AgentRegistry.build(residents={"assistant": assistant}, specialists={})
    assert _render_delegates_block(assistant.delegates, reg) == ""


def test_no_registry_renders_all_delegates_backcompat():
    """Back-compat: with no registry the filter is skipped (legacy behaviour)."""
    assistant = _cfg(
        "assistant", "Ellen",
        delegates=[DelegateEntry(agent="finance", purpose="Money.", when="x")],
    )
    block = _render_delegates_block(assistant.delegates, None)
    assert "finance" in block


def test_agent_registry_is_known():
    reg = AgentRegistry.build(
        residents={"assistant": _cfg("assistant", "Ellen")},
        specialists={"finance": _cfg("finance", "Alex")},
    )
    assert reg.is_known("assistant") is True
    assert reg.is_known("finance") is True
    assert reg.is_known("ghost") is False
