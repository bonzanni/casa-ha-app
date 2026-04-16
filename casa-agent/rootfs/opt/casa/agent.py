"""Core Agent class -- orchestrates SDK, memory, sessions, and channels."""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    SystemMessage,
    TextBlock,
)

from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig
from hooks import block_dangerous_commands, enforce_path_scope
from mcp_registry import McpServerRegistry
from memory import MemoryProvider
from session_registry import SessionRegistry

logger = logging.getLogger(__name__)


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

    async def handle_message(self, msg: BusMessage) -> str | None:
        """Process an inbound message and return the agent reply text.

        This is designed to be registered as a bus handler via
        ``bus.register(agent_name, agent.handle_message)``.
        """
        try:
            return await self._process(msg)
        except Exception:
            logger.exception("Agent '%s' failed to handle message", self.config.name)
            return "Sorry, something went wrong while processing your request."

    # ------------------------------------------------------------------
    # Internal processing pipeline
    # ------------------------------------------------------------------

    async def _process(self, msg: BusMessage) -> str | None:
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

        # Check for an existing session to resume
        existing = self._session_registry.get(channel_key)
        resume_session_id: str | None = None
        if existing:
            resume_session_id = existing.get("sdk_session_id")
            self._session_registry.touch(channel_key)

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

        # 5. Query the SDK -------------------------------------------------
        client = ClaudeSDKClient(options)
        await client.query(user_text)

        response_text = ""
        sdk_session_id: str | None = resume_session_id

        async for sdk_msg in client.receive_response():
            if isinstance(sdk_msg, SystemMessage):
                if getattr(sdk_msg, "subtype", None) == "init":
                    sdk_session_id = getattr(sdk_msg, "session_id", sdk_session_id)
            elif isinstance(sdk_msg, AssistantMessage):
                for block in getattr(sdk_msg, "content", []):
                    if isinstance(block, TextBlock):
                        response_text += block.text

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
            self._session_registry.register(
                channel_key=channel_key,
                agent=self.config.name,
                sdk_session_id=sdk_session_id,
                memory_session_id=memory_session_id or "",
            )

        # 8. Send response via channel ------------------------------------
        if response_text and msg.channel:
            channel = self._channel_manager.get(msg.channel)
            if channel is not None:
                await channel.send(response_text, msg.context)

        return response_text or None
