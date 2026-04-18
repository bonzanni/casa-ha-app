"""Spec 5.2 §8 — Telegram inbound rate limiting.

telegram.* stubs are installed by tests/conftest.py. Drives
TelegramChannel._handle against a fake Update with a fake Application
that records send_message calls.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from bus import BusMessage, MessageBus
from channels.telegram import TelegramChannel
from rate_limit import RateLimiter


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=1)


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _FakeBot()


def _fake_update(chat_id: str, text: str = "hi") -> Any:
    user = types.SimpleNamespace(first_name="User", id=1)
    message = types.SimpleNamespace(
        text=text,
        message_id=42,
    )
    chat = types.SimpleNamespace(id=chat_id)
    return types.SimpleNamespace(
        message=message,
        effective_chat=chat,
        effective_user=user,
    )


async def _drain_bus_once(bus: MessageBus, target: str = "assistant") -> list[BusMessage]:
    """Pull every message currently queued for *target* without running a handler.

    MessageBus stores items as (priority, seq, BusMessage) tuples in an
    asyncio.PriorityQueue; unwrap to return the message list.
    """
    q = bus.queues.get(target)
    if q is None:
        return []
    out: list[BusMessage] = []
    while not q.empty():
        _priority, _seq, msg = q.get_nowait()
        out.append(msg)
        q.task_done()
    return out


async def _noop_handler(_msg: BusMessage) -> None:
    """Placeholder agent handler so bus.queues['assistant'] exists.

    MessageBus.send drops messages for unregistered targets silently;
    registering a no-op handler creates the queue so _drain_bus_once
    can observe whether rate-limit-allowed messages landed.
    """
    return None


@pytest.fixture
def _channel_factory():
    def make(capacity: int) -> tuple[TelegramChannel, MessageBus, _FakeApp]:
        bus = MessageBus()
        bus.register("assistant", _noop_handler)
        limiter = RateLimiter(capacity=capacity, window_s=60.0)
        channel = TelegramChannel(
            bot_token="T",
            chat_id="0",
            default_agent="assistant",
            bus=bus,
            rate_limiter=limiter,
        )
        # Short-circuit _start_typing — no real typing loop in tests.
        channel._start_typing = lambda _chat: None  # type: ignore[assignment]
        app = _FakeApp()
        channel._app = app  # type: ignore[assignment]
        return channel, bus, app
    return make


pytestmark = pytest.mark.asyncio


class TestTelegramRateLimit:
    async def test_burst_up_to_capacity_reaches_bus(self, _channel_factory):
        channel, bus, app = _channel_factory(3)
        for i in range(3):
            await channel._handle(_fake_update("42", f"msg{i}"), None)
        msgs = await _drain_bus_once(bus)
        assert len(msgs) == 3
        assert app.bot.sent == [], "no rate-limit reply when under limit"

    async def test_reject_emits_one_reply_then_drops_silently(
        self, _channel_factory,
    ):
        channel, bus, app = _channel_factory(2)
        # Two admitted.
        await channel._handle(_fake_update("42", "a"), None)
        await channel._handle(_fake_update("42", "b"), None)
        # Next three must all be dropped; only ONE reply sent.
        for i in range(3):
            await channel._handle(_fake_update("42", f"spam{i}"), None)

        msgs = await _drain_bus_once(bus)
        assert len(msgs) == 2, "rejected messages must not reach the bus"

        assert len(app.bot.sent) == 1, (
            f"expected exactly one rate-limit reply, got {len(app.bot.sent)}"
        )
        reply = app.bot.sent[0]
        assert reply["chat_id"] == "42"
        assert "slow down" in reply["text"].lower()

    async def test_bucket_is_per_chat_id(self, _channel_factory):
        channel, bus, app = _channel_factory(1)
        await channel._handle(_fake_update("chat-A", "hi"), None)
        await channel._handle(_fake_update("chat-B", "hi"), None)  # different chat — admitted
        await channel._handle(_fake_update("chat-A", "spam"), None)  # rejected

        msgs = await _drain_bus_once(bus)
        assert len(msgs) == 2
        assert {m.context["chat_id"] for m in msgs} == {"chat-A", "chat-B"}
        # chat-A got a single slow-down reply; chat-B got none.
        assert len(app.bot.sent) == 1
        assert app.bot.sent[0]["chat_id"] == "chat-A"

    async def test_capacity_zero_disables(self, _channel_factory):
        channel, bus, app = _channel_factory(0)
        for i in range(50):
            await channel._handle(_fake_update("42", f"m{i}"), None)
        msgs = await _drain_bus_once(bus)
        assert len(msgs) == 50
        assert app.bot.sent == []

    async def test_no_limiter_is_unlimited(self):
        """Pre-existing callers that don't pass a limiter must keep working."""
        bus = MessageBus()
        bus.register("assistant", _noop_handler)
        channel = TelegramChannel(
            bot_token="T", chat_id="0",
            default_agent="assistant", bus=bus,
            # no rate_limiter kwarg
        )
        channel._start_typing = lambda _chat: None  # type: ignore[assignment]
        channel._app = _FakeApp()  # type: ignore[assignment]

        for i in range(100):
            await channel._handle(_fake_update("42", f"m{i}"), None)
        msgs = await _drain_bus_once(bus)
        assert len(msgs) == 100

    async def test_reply_does_not_start_typing_loop(self, _channel_factory):
        """A rejected message must not even kick off the typing indicator —
        the user sees the reject line, not the typing dots."""
        channel, bus, app = _channel_factory(1)
        started: list[str] = []
        channel._start_typing = lambda chat: started.append(chat)  # type: ignore[assignment]

        await channel._handle(_fake_update("42", "first"), None)
        assert started == ["42"]
        started.clear()

        await channel._handle(_fake_update("42", "second"), None)  # rejected
        assert started == [], "typing indicator must not start on rejected messages"
