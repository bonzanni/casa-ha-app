"""In-process MCP tools for the Casa framework."""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from bus import MessageBus, BusMessage, MessageType
from channels import ChannelManager

logger = logging.getLogger(__name__)

# Module-level references, initialized via init_tools()
_channel_manager: ChannelManager | None = None
_bus: MessageBus | None = None


def init_tools(channel_manager: ChannelManager, bus: MessageBus) -> None:
    """Initialize module-level references used by tool implementations."""
    global _channel_manager, _bus  # noqa: PLW0603
    _channel_manager = channel_manager
    _bus = bus


@tool(
    "send_message",
    "Send a message to a user through a communication channel.",
    {"message": str, "channel": str},
)
async def send_message(args: dict) -> dict:
    """Send a message through a named channel."""
    message = args.get("message", "")
    channel = args.get("channel", "telegram")

    if _channel_manager is None:
        return {"content": [{"type": "text", "text": "Error: tools not initialized"}]}

    ch = _channel_manager.get(channel)
    if ch is None:
        return {"content": [{"type": "text", "text": f"Error: channel '{channel}' not found"}]}

    await ch.send(message, {})
    return {"content": [{"type": "text", "text": f"Message sent via {channel}."}]}


def create_casa_tools() -> dict[str, Any]:
    """Create and return the casa-framework MCP server config.

    The returned dict can be registered with :class:`McpServerRegistry`
    via ``register_sdk``.
    """
    server = create_sdk_mcp_server(
        name="casa-framework",
        tools=[send_message],
    )
    return server
