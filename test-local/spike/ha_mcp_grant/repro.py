"""Spike: does claude_agent_sdk 0.1.61 honor `mcp__<server>` server-level
allow-list entries, or does it require `mcp__<server>__<tool>` per-tool
entries?

Run inside the casa-test container so the SDK version matches production.

The SDK itself is a transport layer: it joins `allowed_tools` with `,` and
forwards verbatim to the Claude Code CLI via `--allowedTools` (see
claude_agent_sdk/_internal/transport/subprocess_cli.py:196-197). Server-level
matching is therefore a CLI-side decision; this script confirms only that
the SDK constructor accepts both shapes without validation error.
"""
from __future__ import annotations

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions

print(f"claude_agent_sdk version: {claude_agent_sdk.__version__}")

# Form A: server-level grant
opts_a = ClaudeAgentOptions(
    model="claude-haiku-4-5-20251001",
    allowed_tools=["mcp__homeassistant"],
    mcp_servers={"homeassistant": {"type": "http", "url": "http://example/"}},
)
print("Form A constructed OK:", opts_a.allowed_tools)

# Form B: per-tool grant
opts_b = ClaudeAgentOptions(
    model="claude-haiku-4-5-20251001",
    allowed_tools=["mcp__homeassistant__HassTurnOff"],
    mcp_servers={"homeassistant": {"type": "http", "url": "http://example/"}},
)
print("Form B constructed OK:", opts_b.allowed_tools)
