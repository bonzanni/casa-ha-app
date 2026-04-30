"""in_casa driver — embedded claude_agent_sdk engagement runtime."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from drivers.driver_protocol import DriverProtocol
from engagement_registry import EngagementRecord
import sdk_logging

if TYPE_CHECKING:
    from channels.telegram import TopicStreamHandle

logger = logging.getLogger(__name__)


TopicStreamFactory = Callable[[int], "TopicStreamHandle"]
"""(topic_id) → TopicStreamHandle — channel-side per-turn streaming primitive.

Returned handle exposes async ``emit(accumulated_text)`` and async
``finalize(full_text)``. See channels.telegram.TopicStreamHandle."""

SessionIdPersister = Callable[[str, str], Awaitable[None]]
"""(engagement_id, session_id) → None — registry persist hook.

Matches engagement_registry.persist_session_id's bound-method signature."""


class DriverNotAliveError(RuntimeError):
    """Raised when a turn is fed to a driver that has no open client."""


class InCasaDriver(DriverProtocol):
    """Holds one ClaudeSDKClient per active engagement.

    ``topic_stream_factory`` is the channel-side factory that, given a
    topic_id, returns a TopicStreamHandle. Each ``_deliver_turn`` builds
    a fresh handle and emits AssistantMessage chunks progressively
    (Phase 3b — Bug 1). Injected rather than imported from
    ``channels.telegram`` to keep the driver pure/testable.
    """

    def __init__(
        self,
        *,
        topic_stream_factory: TopicStreamFactory,
        persist_session_id: SessionIdPersister | None = None,
    ) -> None:
        self._topic_stream_factory = topic_stream_factory
        self._persist_session_id = persist_session_id
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
        client = ClaudeSDKClient(
            sdk_logging.with_stderr_callback(
                options, engagement_id=engagement.id[:8],
            ),
        )
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
        client = ClaudeSDKClient(
            sdk_logging.with_stderr_callback(
                options, engagement_id=engagement.id[:8],
            ),
        )
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
        # Lazy import: tools imports engagement_registry; doing this at
        # module top-level would create a circular import.
        from tools import engagement_var

        client = self._clients[engagement.id]
        lock = self._locks[engagement.id]
        assert engagement.topic_id is not None
        # Phase 3b: stream per-AssistantMessage rather than buffer the
        # entire turn.
        stream = self._topic_stream_factory(engagement.topic_id)
        accumulated = ""
        idx = 0  # Phase 4b: per-turn AssistantMessage counter.
        started_ms = time.monotonic() * 1000  # Phase 4b: turn duration anchor.
        # Per-call tool name lookup so log_tool_result can render name=.
        tool_names_by_id: dict[str, str] = {}
        token = engagement_var.set(engagement)
        try:
            async with lock:
                await client.query(prompt)
                async for sdk_msg in client.receive_response():
                    sid = getattr(client, "session_id", None)
                    if (
                        sid
                        and self._persist_session_id is not None
                        and engagement.sdk_session_id != sid
                    ):
                        try:
                            await self._persist_session_id(engagement.id, sid)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "Engagement %s persist_session_id failed: %s",
                                engagement.id[:8], exc,
                            )
                        engagement.sdk_session_id = sid
                    # Phase 4b dispatch — wrapped in try/except so a
                    # malformed block does not abort the rest of the turn.
                    try:
                        if isinstance(sdk_msg, SystemMessage):
                            sdk_logging.log_system_init(sdk_msg)
                        elif isinstance(sdk_msg, AssistantMessage):
                            idx += 1
                            sdk_logging.log_assistant_message(sdk_msg, idx=idx)
                            for block in getattr(sdk_msg, "content", []) or []:
                                if isinstance(block, ToolUseBlock):
                                    tool_names_by_id[
                                        getattr(block, "id", "")
                                    ] = getattr(block, "name", "?")
                                    sdk_logging.log_tool_use(block, idx=idx)
                        elif isinstance(sdk_msg, UserMessage):
                            for block in getattr(sdk_msg, "content", []) or []:
                                if isinstance(block, ToolResultBlock):
                                    name = tool_names_by_id.get(
                                        getattr(block, "tool_use_id", ""),
                                        "",
                                    )
                                    sdk_logging.log_tool_result(
                                        block, idx=idx, started_ms=started_ms,
                                        name=name,
                                    )
                        elif isinstance(sdk_msg, ResultMessage):
                            sdk_logging.log_turn_done(
                                sdk_msg, started_ms=started_ms,
                            )
                    except Exception as dispatch_exc:  # noqa: BLE001
                        logger.warning(
                            "phase4b dispatch failed: %s", dispatch_exc,
                            exc_info=True,
                        )
                    # Phase 3b streaming — unchanged.
                    if isinstance(sdk_msg, AssistantMessage):
                        msg_text = "".join(
                            b.text for b in getattr(sdk_msg, "content", [])
                            if isinstance(b, TextBlock)
                        )
                        if msg_text:
                            accumulated = (
                                f"{accumulated}\n\n{msg_text}"
                                if accumulated else msg_text
                            )
                            await stream.emit(accumulated)
        finally:
            engagement_var.reset(token)
        final = accumulated.strip()
        if final:
            await stream.finalize(final)
