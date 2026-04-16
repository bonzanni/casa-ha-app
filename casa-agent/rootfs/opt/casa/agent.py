"""Core Agent class -- orchestrates SDK, memory, sessions, and channels."""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    SystemMessage,
    TextBlock,
)

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from config import AgentConfig
from hooks import block_dangerous_commands, enforce_path_scope
from mcp_registry import McpServerRegistry
from memory import MemoryProvider
from session_registry import SessionRegistry

logger = logging.getLogger(__name__)

# Type alias for the streaming callback
OnTokenCallback = Callable[[str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class ErrorKind(Enum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SDK_ERROR = "sdk_error"
    MEMORY_ERROR = "memory_error"
    CHANNEL_ERROR = "channel_error"
    UNKNOWN = "unknown"


_USER_MESSAGES: dict[ErrorKind, str] = {
    ErrorKind.TIMEOUT: "The request timed out. Try again in a moment.",
    ErrorKind.RATE_LIMIT: "Rate limited by the API. Please wait a minute and try again.",
    ErrorKind.SDK_ERROR: "There was an issue communicating with Claude. Please try again.",
    ErrorKind.MEMORY_ERROR: "Memory service is unavailable, but I can still respond without context.",
    ErrorKind.CHANNEL_ERROR: "There was an issue sending the response.",
    ErrorKind.UNKNOWN: "Sorry, something went wrong while processing your request.",
}


def _classify_error(exc: Exception) -> ErrorKind:
    """Classify an exception into an ErrorKind for routing recovery."""
    if isinstance(exc, asyncio.TimeoutError):
        return ErrorKind.TIMEOUT

    msg = str(exc).lower()
    if "rate" in msg and "limit" in msg:
        return ErrorKind.RATE_LIMIT
    if "429" in msg:
        return ErrorKind.RATE_LIMIT
    if "timeout" in msg or "timed out" in msg:
        return ErrorKind.TIMEOUT

    exc_type = type(exc).__name__
    if "CLI" in exc_type or "SDK" in exc_type or "Connection" in exc_type:
        return ErrorKind.SDK_ERROR

    return ErrorKind.UNKNOWN


class Agent:
    """A Casa agent backed by the Claude Agent SDK."""

    def __init__(
        self,
        config: AgentConfig,
        memory: MemoryProvider,
        session_registry: SessionRegistry,
        mcp_registry: McpServerRegistry,
        channel_manager: ChannelManager,
    ) -> None:
        self.config = config
        self._memory = memory
        self._session_registry = session_registry
        self._mcp_registry = mcp_registry
        self._channel_manager = channel_manager

    # ------------------------------------------------------------------
    # Public entry point (used as bus handler)
    # ------------------------------------------------------------------

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        """Process an inbound message and return a response BusMessage.

        If the channel supports streaming, tokens are delivered
        incrementally via ``on_token``.  The full response is sent or
        finalized after the SDK completes.
        """
        # Obtain a streaming callback from the channel (if available)
        on_token: OnTokenCallback | None = None
        channel = self._channel_manager.get(msg.channel) if msg.channel else None

        if channel is not None and hasattr(channel, "create_on_token"):
            on_token = channel.create_on_token(msg.context)

        try:
            text = await self._process(msg, on_token=on_token)
        except Exception as exc:
            kind = _classify_error(exc)
            logger.error(
                "Agent '%s' error [%s]: %s",
                self.config.name,
                kind.value,
                exc,
                exc_info=(kind == ErrorKind.UNKNOWN),
            )
            text = _USER_MESSAGES[kind]

        # Deliver final response via the channel
        if text and channel is not None:
            if on_token is not None and hasattr(channel, "finalize_stream"):
                await channel.finalize_stream(text, msg.context, on_token)
            else:
                await channel.send(text, msg.context)

        if text is None:
            return None

        return BusMessage(
            type=MessageType.RESPONSE,
            source=self.config.name.lower(),
            target=msg.source,
            content=text,
            reply_to=msg.id,
            channel=msg.channel,
            context=msg.context,
        )

    # ------------------------------------------------------------------
    # Internal processing pipeline
    # ------------------------------------------------------------------

    async def _process(
        self,
        msg: BusMessage,
        on_token: OnTokenCallback | None = None,
    ) -> str | None:
        channel_key = f"{msg.channel}:{msg.context.get('chat_id', 'default')}"
        user_text = str(msg.content)

        # 1. Memory context ------------------------------------------------
        memory_context = ""
        try:
            memory_context = await self._memory.get_context(
                peer_id=self.config.memory.peer_name,
                token_budget=self.config.memory.token_budget,
                exclude_tags=self.config.memory.exclude_tags or None,
            )
        except Exception:
            logger.warning("Memory retrieval failed; proceeding without context")

        # 2. System prompt -------------------------------------------------
        system_parts = [self.config.personality]
        if memory_context:
            system_parts.append(
                f"\n<memory_context>\n{memory_context}\n</memory_context>"
            )
        system_prompt = "\n".join(system_parts)

        # 3. MCP servers ---------------------------------------------------
        mcp_servers = self._mcp_registry.resolve(self.config.mcp_server_names)

        # 4. Build SDK options ---------------------------------------------
        hooks = {
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[block_dangerous_commands]),
                HookMatcher(matcher="Read|Write|Edit", hooks=[enforce_path_scope]),
            ],
        }

        existing = self._session_registry.get(channel_key)
        resume_session_id: str | None = None
        if existing:
            resume_session_id = existing.get("sdk_session_id")
            await self._session_registry.touch(channel_key)

        options = ClaudeAgentOptions(
            model=self.config.model,
            system_prompt=system_prompt,
            allowed_tools=self.config.tools.allowed,
            disallowed_tools=self.config.tools.disallowed,
            permission_mode=self.config.tools.permission_mode or "acceptEdits",
            max_turns=self.config.tools.max_turns,
            mcp_servers=mcp_servers if mcp_servers else {},
            hooks=hooks,
            cwd=self.config.cwd or None,
            resume=resume_session_id,
        )

        # 5. Query the SDK (stream tokens to callback) ---------------------
        response_text = ""
        sdk_session_id: str | None = resume_session_id

        async with ClaudeSDKClient(options) as client:
            await client.query(user_text)

            async for sdk_msg in client.receive_response():
                if isinstance(sdk_msg, SystemMessage):
                    if getattr(sdk_msg, "subtype", None) == "init":
                        sdk_session_id = getattr(sdk_msg, "session_id", sdk_session_id)
                elif isinstance(sdk_msg, AssistantMessage):
                    for block in getattr(sdk_msg, "content", []):
                        if isinstance(block, TextBlock):
                            response_text += block.text
                            if on_token is not None:
                                await on_token(response_text)

        # 6. Store in memory -----------------------------------------------
        memory_session_id: str | None = None
        if existing:
            memory_session_id = existing.get("memory_session_id")

        try:
            if not memory_session_id:
                memory_session_id = await self._memory.create_session(
                    self.config.memory.peer_name
                )
            await self._memory.store_message(
                session_id=memory_session_id,
                peer_id=self.config.memory.peer_name,
                content=user_text,
                role="user",
            )
            if response_text:
                await self._memory.store_message(
                    session_id=memory_session_id,
                    peer_id=self.config.memory.peer_name,
                    content=response_text,
                    role="assistant",
                )
        except Exception:
            logger.warning("Memory storage failed; response still delivered")

        # 7. Update session registry ---------------------------------------
        if sdk_session_id:
            await self._session_registry.register(
                channel_key=channel_key,
                agent=self.config.name,
                sdk_session_id=sdk_session_id,
                memory_session_id=memory_session_id or "",
            )

        # NOTE: channel send is handled by handle_message, not here.
        return response_text or None
