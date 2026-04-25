"""Tests for the bidirectional name↔role registry."""

from __future__ import annotations

import pytest

from agent_registry import AgentRegistry, KnownAgent
from config import AgentConfig, CharacterConfig

pytestmark = pytest.mark.asyncio


def _cfg(role: str, name: str, card: str = "") -> AgentConfig:
    return AgentConfig(
        role=role,
        character=CharacterConfig(name=name, card=card),
    )


def test_role_to_name_basic():
    reg = AgentRegistry.build(
        residents={"assistant": _cfg("assistant", "Ellen")},
        specialists={"finance": _cfg("finance", "Alex")},
    )
    assert reg.role_to_name("assistant") == "Ellen"
    assert reg.role_to_name("finance") == "Alex"


def test_name_to_role_case_insensitive():
    reg = AgentRegistry.build(
        residents={"butler": _cfg("butler", "Tina")},
        specialists={},
    )
    assert reg.name_to_role("Tina") == "butler"
    assert reg.name_to_role("tina") == "butler"
    assert reg.name_to_role("TINA") == "butler"


def test_name_to_role_unknown_returns_none():
    reg = AgentRegistry.build(residents={}, specialists={})
    assert reg.name_to_role("nobody") is None


def test_role_to_name_unknown_returns_role_itself():
    """Fallback so prompt rendering never blows up if a config is malformed."""
    reg = AgentRegistry.build(residents={}, specialists={})
    assert reg.role_to_name("ghost") == "ghost"


def test_all_known_returns_residents_and_specialists_with_tier():
    reg = AgentRegistry.build(
        residents={"assistant": _cfg("assistant", "Ellen", card="primary")},
        specialists={"finance": _cfg("finance", "Alex", card="money")},
    )
    known = {k.role: k for k in reg.all_known()}
    assert known["assistant"].name == "Ellen"
    assert known["assistant"].tier == "resident"
    assert known["assistant"].card == "primary"
    assert known["finance"].tier == "specialist"
