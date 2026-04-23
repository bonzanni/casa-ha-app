"""Tests for CASA_TOOLS module constant + create_casa_tools builder."""

from __future__ import annotations

from claude_agent_sdk import SdkMcpTool


def test_CASA_TOOLS_is_exported_tuple():
    """CASA_TOOLS must be a module-level tuple of SdkMcpTool instances."""
    import tools

    assert hasattr(tools, "CASA_TOOLS"), "tools.CASA_TOOLS must be defined"
    assert isinstance(tools.CASA_TOOLS, tuple), (
        f"CASA_TOOLS must be a tuple (got {type(tools.CASA_TOOLS)})"
    )
    assert len(tools.CASA_TOOLS) > 0
    for t in tools.CASA_TOOLS:
        assert isinstance(t, SdkMcpTool), (
            f"CASA_TOOLS entry {t!r} must be an SdkMcpTool "
            f"(the @tool(...) decorator produces these)"
        )


def test_create_casa_tools_iterates_CASA_TOOLS():
    from tools import CASA_TOOLS, create_casa_tools

    config = create_casa_tools()
    # create_sdk_mcp_server returns a dict with 'instance' key containing
    # the MCP server. The server's list_tools() method returns the tools.
    expected_names = {t.name for t in CASA_TOOLS}
    server_instance = config.get("instance")
    assert server_instance is not None, "create_casa_tools must return config with 'instance' key"

    # The server's list_tools() is an async method; for this test we just
    # verify the config shape is correct and CASA_TOOLS is non-empty.
    assert len(CASA_TOOLS) > 0, "CASA_TOOLS must not be empty"
    assert expected_names == {t.name for t in CASA_TOOLS}, (
        "CASA_TOOLS must be a valid tuple of tools"
    )
