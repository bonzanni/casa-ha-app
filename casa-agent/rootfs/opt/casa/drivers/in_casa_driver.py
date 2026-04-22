"""in_casa driver — embedded claude_agent_sdk engagement runtime."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

from drivers.driver_protocol import DriverProtocol
from engagement_registry import EngagementRecord

logger = logging.getLogger(__name__)


TopicSender = Callable[[int, str], Awaitable[None]]
"""(topic_id, text) → None — the channel-side async post."""


class DriverNotAliveError(RuntimeError):
    """Raised when a turn is fed to a driver that has no open client."""


class InCasaDriver(DriverProtocol):
    """Holds one ClaudeSDKClient per active engagement.

    ``send_to_topic`` is the channel-side callback used to stream responses
    into the Telegram topic (or the mock in tests). Injected rather than
    imported from ``channels.telegram`` to keep the driver pure/testable.
    """

    def __init__(self, *, send_to_topic: TopicSender) -> None:
        self._send_to_topic = send_to_topic
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._ctx_stack: dict[str, Any] = {}
        # Per-engagement asyncio.Lock guards query/receive_response sequencing:
        # ClaudeSDKClient is single-threaded per connection.
        self._locks: dict[str, asyncio.Lock] = {}

    # -- lifecycle --------------------------------------------------------

    async def start(
        self,
        engagement: EngagementRecord,
        prompt: str,
        options: ClaudeAgentOptions,
    ) -> None:
        assert engagement.topic_id is not None, (
            "in_casa driver requires a topic_id (got None)"
        )
        client = ClaudeSDKClient(options)
        ctx = client.__aenter__()
        entered = await ctx if asyncio.iscoroutine(ctx) else ctx
        self._clients[engagement.id] = entered or client
        self._ctx_stack[engagement.id] = client  # for __aexit__
        self._locks[engagement.id] = asyncio.Lock()
        logger.info(
            "Engagement %s driver=in_casa client opened", engagement.id[:8],
        )
        await self._deliver_turn(engagement, prompt)

    async def send_user_turn(
        self, engagement: EngagementRecord, text: str,
    ) -> None:
        if not self.is_alive(engagement):
            raise DriverNotAliveError(
                f"engagement {engagement.id[:8]} has no live client"
            )
        await self._deliver_turn(engagement, text)

    async def cancel(self, engagement: EngagementRecord) -> None:
        client = self._clients.pop(engagement.id, None)
        ctx = self._ctx_stack.pop(engagement.id, None)
        self._locks.pop(engagement.id, None)
        if client is None and ctx is None:
            return
        try:
            # Prefer close() on the entered client; fall back to __aexit__ on
            # the original context manager object if close() is absent.
            if client is not None and hasattr(client, "close"):
                await client.close()
            elif ctx is not None and hasattr(ctx, "__aexit__"):
                await ctx.__aexit__(None, None, None)
        except Exception as exc:
            logger.warning(
                "Engagement %s cancel: client close raised %s",
                engagement.id[:8], exc,
            )

    async def resume(
        self, engagement: EngagementRecord, session_id: str,
    ) -> None:
        """Reopen a ClaudeSDKClient with resume=session_id.

        Caller (telegram routing path, after user turn in a suspended topic)
        handles retry + error surfacing. This method raises on failure.
        """
        if self.is_alive(engagement):
            logger.warning(
                "resume() called on engagement %s that is already alive",
                engagement.id[:8],
            )
            return
        options = ClaudeAgentOptions(resume=session_id)
        client = ClaudeSDKClient(options)
        entered = await client.__aenter__()
        self._clients[engagement.id] = entered or client
        self._ctx_stack[engagement.id] = client
        self._locks[engagement.id] = asyncio.Lock()
        logger.info(
            "Engagement %s resumed (session=%s)",
            engagement.id[:8], session_id,
        )

    def get_session_id(self, engagement: EngagementRecord) -> str | None:
        """Return the live client's session_id for persistence before cancel."""
        client = self._clients.get(engagement.id)
        return getattr(client, "session_id", None) if client else None

    def is_alive(self, engagement: EngagementRecord) -> bool:
        return engagement.id in self._clients

    # -- internal ---------------------------------------------------------

    async def _deliver_turn(
        self, engagement: EngagementRecord, prompt: str,
    ) -> None:
        client = self._clients[engagement.id]
        lock = self._locks[engagement.id]
        chunks: list[str] = []
        async with lock:
            await client.query(prompt)
            async for sdk_msg in client.receive_response():
                if isinstance(sdk_msg, AssistantMessage):
                    for block in getattr(sdk_msg, "content", []):
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
        text = "".join(chunks).strip()
        if text and engagement.topic_id is not None:
            await self._send_to_topic(engagement.topic_id, text)
