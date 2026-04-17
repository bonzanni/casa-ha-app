"""Lightweight async message bus for inter-agent communication."""

from __future__ import annotations

import asyncio
import collections
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class MessageType(Enum):
    REQUEST = "request"
    RESPONSE = "response"
    NOTIFICATION = "notification"
    CHANNEL_IN = "channel_in"
    CHANNEL_OUT = "channel_out"
    SCHEDULED = "scheduled"


@dataclass
class BusMessage:
    type: MessageType
    source: str
    target: str
    content: Any
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    reply_to: str = ""
    channel: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    priority: int = 1


# Type alias for handler callbacks
Handler = Callable[[BusMessage], Coroutine[Any, Any, BusMessage | None]]


class MessageBus:
    """Simple priority-queue-based message bus."""

    def __init__(self, max_log_size: int = 10000) -> None:
        self.queues: dict[str, asyncio.PriorityQueue] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.handlers: dict[str, Handler | None] = {}
        # msg_id -> dispatch task. Populated by run_agent_loop when a
        # REQUEST is picked up; consulted by request() so a cancelled
        # caller tears down the in-flight handler task (voice `cancel`
        # frame semantics — spec §10.3).
        self._dispatch_tasks: dict[str, asyncio.Task] = {}
        self._log: collections.deque[BusMessage] = collections.deque(
            maxlen=max_log_size
        )
        self._seq: int = 0

    def register(self, name: str, handler: Handler | None = None) -> None:
        """Register an agent (queue + optional handler)."""
        self.queues[name] = asyncio.PriorityQueue()
        self.handlers[name] = handler

    async def send(self, msg: BusMessage) -> None:
        """Send a message to msg.target's queue."""
        if msg.target not in self.queues:
            return  # silently drop for unknown targets
        self._log.append(msg)
        self._seq += 1
        await self.queues[msg.target].put((msg.priority, self._seq, msg))

    async def request(
        self, msg: BusMessage, timeout: float = 300
    ) -> BusMessage:
        """Send a REQUEST and await the response (or timeout).

        If the caller is cancelled mid-flight, the in-flight dispatch
        task (if registered by ``run_agent_loop``) is also cancelled so
        the handler stops doing work. This is the cancel contract the
        voice channel relies on (spec §10.3).
        """
        msg.type = MessageType.REQUEST
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[BusMessage] = loop.create_future()
        self.pending[msg.id] = fut
        await self.send(msg)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending.pop(msg.id, None)
            raise
        except asyncio.CancelledError:
            self.pending.pop(msg.id, None)
            dispatch = self._dispatch_tasks.get(msg.id)
            if dispatch is not None and not dispatch.done():
                dispatch.cancel()
            raise

    async def respond(self, original_id: str, response: BusMessage) -> None:
        """Complete a pending request future."""
        fut = self.pending.pop(original_id, None)
        if fut and not fut.done():
            fut.set_result(response)

    async def notify(self, msg: BusMessage) -> None:
        """Send a NOTIFICATION (fire-and-forget)."""
        msg.type = MessageType.NOTIFICATION
        await self.send(msg)

    async def run_agent_loop(self, agent_name: str) -> None:
        """Pull messages from the agent's queue and dispatch to its handler.

        Each message is dispatched as an independent asyncio task so that
        multiple concurrent messages are processed in parallel (no implicit
        serialisation at the bus level).  Runs until the current task is
        cancelled.
        """
        queue = self.queues[agent_name]

        async def _dispatch(msg: BusMessage) -> None:
            # Resolve the handler per-message so tests (and future hot
            # reloads) can rebind ``bus.handlers[name]`` and have the
            # next dispatch pick up the new handler.
            handler = self.handlers.get(agent_name)
            try:
                if handler is not None:
                    result = await handler(msg)
                    # Auto-respond to REQUESTs if handler returned something
                    if msg.type == MessageType.REQUEST and result is not None:
                        result.reply_to = msg.id
                        result.type = MessageType.RESPONSE
                        await self.respond(msg.id, result)
            except asyncio.CancelledError:
                # Caller of bus.request cancelled us. Drop the pending
                # future so nobody waits forever; do not synthesise an
                # error response (the caller is already gone).
                self.pending.pop(msg.id, None)
                raise
            except Exception:
                logger.exception(
                    "Bus handler for target=%r raised on msg id=%s",
                    msg.target,
                    msg.id,
                )
                # Unblock REQUEST callers so they don't wait for the full timeout.
                if msg.type == MessageType.REQUEST:
                    error_resp = BusMessage(
                        type=MessageType.RESPONSE,
                        source=msg.target,
                        target=msg.source,
                        content=f"handler error: {msg.id}",
                        reply_to=msg.id,
                    )
                    await self.respond(msg.id, error_resp)
            finally:
                if msg.type == MessageType.REQUEST:
                    self._dispatch_tasks.pop(msg.id, None)
                queue.task_done()

        while True:
            _priority, _seq, msg = await queue.get()
            task = asyncio.create_task(_dispatch(msg))
            if msg.type == MessageType.REQUEST:
                self._dispatch_tasks[msg.id] = task

    def get_log(self, last_n: int = 50) -> list[BusMessage]:
        """Return the last *last_n* log entries."""
        items = list(self._log)
        return items[-last_n:]
