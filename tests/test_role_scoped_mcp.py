"""Role-scoped MCP registry and Casa-framework schema selection tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_role_sdk_override_wins_only_for_target_role():
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_http("homeassistant", "http://raw")
    registry.register_role_sdk(
        "homeassistant", "butler", {"type": "sdk", "instance": object()},
    )

    assert registry.resolve(
        ["homeassistant"], role="butler",
    )["homeassistant"]["type"] == "sdk"
    assert registry.resolve(
        ["homeassistant"], role="assistant",
    )["homeassistant"]["url"] == "http://raw"


def test_sdk_factory_receives_role_and_frozen_grants():
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    registry.register_sdk_factory(
        "casa-framework",
        lambda role, grants: {
            "type": "sdk", "role": role, "grants": grants,
        },
    )

    cfg = registry.resolve(
        ["casa-framework"],
        role="butler",
        allowed_tools=["mcp__casa-framework__get_schedule"],
    )["casa-framework"]

    assert cfg["role"] == "butler"
    assert cfg["grants"] == frozenset({
        "mcp__casa-framework__get_schedule",
    })


def test_casa_framework_factory_exposes_only_granted_tools():
    from tools import create_casa_tools, select_casa_tools

    grants = frozenset({
        "mcp__casa-framework__get_schedule",
        "mcp__casa-framework__recall_memory",
    })

    cfg = create_casa_tools(grants)

    assert {tool.name for tool in select_casa_tools(grants)} == {
        "get_schedule", "recall_memory",
    }
    assert cfg["alwaysLoad"] is True


def test_server_level_framework_grant_keeps_full_surface():
    from tools import CASA_TOOLS, create_casa_tools, select_casa_tools

    grants = frozenset({"mcp__casa-framework"})

    cfg = create_casa_tools(grants)

    assert {tool.name for tool in select_casa_tools(grants)} == {
        tool.name for tool in CASA_TOOLS
    }
    assert cfg["alwaysLoad"] is True
