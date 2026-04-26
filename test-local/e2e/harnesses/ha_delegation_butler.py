"""H-3 harness for test_ha_delegation.sh.

Loads butler from the bundled defaults via agent_loader (the same path
casa_core uses on boot), constructs SDK options the same way, and runs
one query. The mock SDK reads /data/mock_sdk_tool_invoke.json and fires
a real HTTP POST against the resolved homeassistant URL — proving the
runtime.yaml → registry → SDK options chain actually surfaces the URL
that CASA_HA_MCP_URL set.

Run inside the casa-test container's venv:
    /opt/casa/venv/bin/python /tmp/ha_delegation_butler.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, "/opt/casa")

from agent_loader import load_agent_from_dir
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from mcp_registry import McpServerRegistry
from policies import load_policies


async def main() -> None:
    # 1. Load butler from defaults — same path casa_core uses.
    policies = load_policies("/opt/casa/defaults/policies/disclosure.yaml")
    cfg = load_agent_from_dir(
        "/opt/casa/defaults/agents/butler",
        policies=policies,
    )
    assert "mcp__homeassistant" in cfg.tools.allowed, (
        f"butler.tools.allowed missing mcp__homeassistant: {cfg.tools.allowed}"
    )
    assert "homeassistant" in cfg.mcp_server_names, (
        f"butler.mcp_server_names missing homeassistant: {cfg.mcp_server_names}"
    )

    # 2. Build the same registry casa_core does and resolve butler's MCP servers.
    reg = McpServerRegistry()
    reg.register_http(
        name="homeassistant",
        url=os.environ.get("CASA_HA_MCP_URL"),
        headers={"Authorization": "Bearer test-token-v0151"},
    )
    resolved = reg.resolve(cfg.mcp_server_names)
    assert "homeassistant" in resolved and resolved["homeassistant"]["url"], (
        f"registry.resolve did not surface mock URL: {resolved}"
    )

    # 3. Construct SDK options the way casa_core does for residents.
    opts = ClaudeAgentOptions(
        model=cfg.model or "haiku",
        system_prompt=cfg.system_prompt or "",
        allowed_tools=list(cfg.tools.allowed),
        mcp_servers=resolved,
        max_turns=1,
    )

    # 4. Run a query — mock SDK reads /data/mock_sdk_tool_invoke.json and
    #    fires the HTTP call against the resolved homeassistant URL.
    async with ClaudeSDKClient(opts) as client:
        await client.query("turn on the bedroom light")
        async for _msg in client.receive_response():
            pass

    print("OK")


asyncio.run(main())
