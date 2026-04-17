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
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from config import AgentConfig
from hooks import block_dangerous_commands, make_path_scope_hook
from mcp_registry import McpServerRegistry
from channel_trust import channel_trust, user_peer_for_channel
from memory import MemoryProvider
from session_registry import SessionRegistry, build_session_key

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
        self._bg_tasks: set[asyncio.Task] = set()

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
            source=self.config.role,
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
        channel_key = build_session_key(
            msg.channel,
            msg.context.get("chat_id"),
        )
        session_id = f"{channel_key}:{self.config.role}"
        user_peer = user_peer_for_channel(msg.channel)
        user_text = str(msg.content)

        # 1. Ensure session + peers (idempotent, cheap when warm). ----------
        try:
            await self._memory.ensure_session(
                session_id=session_id,
                agent_role=self.config.role,
                user_peer=user_peer,
            )
        except Exception:
            logger.warning(
                "Memory ensure_session failed; continuing without memory",
            )

        # 2. Retrieve memory digest. ---------------------------------------
        memory_context = ""
        try:
            memory_context = await self._memory.get_context(
                session_id=session_id,
                agent_role=self.config.role,
                tokens=self.config.memory.token_budget,
                search_query=user_text,
                user_peer=user_peer,
            )
        except Exception:
            logger.warning(
                "Memory retrieval failed; proceeding without context",
            )

        # 3. System prompt = personality + <memory_context>? + <channel_context>.
        system_parts = [self.config.personality]
        if memory_context:
            system_parts.append(
                f"\n<memory_context>\n{memory_context}\n</memory_context>"
            )
        system_parts.append(
            "\n<channel_context>\n"
            f"channel: {msg.channel}\n"
            f"trust: {channel_trust(msg.channel)}\n"
            "</channel_context>"
        )
        system_prompt = "\n".join(system_parts)

        # 4. MCP servers ---------------------------------------------------
        mcp_servers = self._mcp_registry.resolve(self.config.mcp_server_names)

        # 5. Hooks (agent-identity captured in closures). ------------------
        hooks = {
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[block_dangerous_commands]),
                HookMatcher(
                    matcher="Read|Write|Edit",
                    hooks=[make_path_scope_hook(self.config.role)],
                ),
            ],
        }

        # 6. SDK resume --------------------------------------------------
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
            setting_sources=["project"],
        )

        # 7. Query the SDK. ------------------------------------------------
        response_text = ""
        sdk_session_id: str | None = resume_session_id

        async with ClaudeSDKClient(options) as client:
            await client.query(user_text)
            async for sdk_msg in client.receive_response():
                if isinstance(sdk_msg, SystemMessage):
                    if getattr(sdk_msg, "subtype", None) == "init":
                        data = getattr(sdk_msg, "data", {}) or {}
                        if "session_id" in data:
                            sdk_session_id = data["session_id"]
                elif isinstance(sdk_msg, ResultMessage):
                    sid = getattr(sdk_msg, "session_id", None)
                    if sid:
                        sdk_session_id = sid
                elif isinstance(sdk_msg, AssistantMessage):
                    for block in getattr(sdk_msg, "content", []):
                        if isinstance(block, TextBlock):
                            response_text += block.text
                            if on_token is not None:
                                await on_token(response_text)

        if sdk_session_id and sdk_session_id != resume_session_id:
            logger.info(
                "SDK session for '%s': %s",
                self.config.role,
                sdk_session_id,
            )

        # 8. Persist — off the critical path. Storage is unconditional. ---
        #    Session+peer topology already scopes visibility (spec §4.3).
        if response_text:
            task = asyncio.create_task(self._add_turn_bg(
                session_id, self.config.role, user_text, response_text, user_peer,
            ))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

        # 9. SessionRegistry — only SDK session id now. --------------------
        if sdk_session_id:
            await self._session_registry.register(
                channel_key=channel_key,
                agent=self.config.role,
                sdk_session_id=sdk_session_id,
            )

        return response_text or None

    async def _add_turn_bg(
        self,
        session_id: str,
        agent_role: str,
        user_text: str,
        assistant_text: str,
        user_peer: str,
    ) -> None:
        """Persist a turn in the background. Exceptions are caught and
        logged — never surfaced to the user (the response has already
        been delivered). Spec §11."""
        try:
            await self._memory.add_turn(
                session_id=session_id,
                agent_role=agent_role,
                user_text=user_text,
                assistant_text=assistant_text,
                user_peer=user_peer,
            )
        except Exception as exc:
            logger.warning(
                "Memory add_turn failed in background: %s", exc,
            )
