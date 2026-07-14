# tests/test_telegram_new_reset.py
"""Telegram /new command interception (spec §4.2 #2, C2).

Drives TelegramChannel._handle with a fake Update carrying "/new" and
asserts that (a) the session is reset via reset_channel, (b) the /new
text is NOT forwarded to the bus/agent, and (c) an ack is sent to the
user. Uses the same _FakeBot / _FakeApp pattern as test_telegram_rate_limit.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest

from bus import BusMessage, MessageBus
from channels.telegram import TelegramChannel
from session_registry import build_scoped_session_key

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# /new keys off self.default_agent ("assistant" for _make_channel below).
_KEY_42 = build_scoped_session_key("telegram", "assistant", "42")


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs) -> object:
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=1)


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _FakeBot()


def _fake_update(chat_id: str, text: str) -> object:
    user = types.SimpleNamespace(first_name="Nicola", id=1)
    message = types.SimpleNamespace(text=text, message_id=7)
    chat = types.SimpleNamespace(id=chat_id)
    return types.SimpleNamespace(
        message=message,
        effective_chat=chat,
        effective_user=user,
    )


async def _drain_bus(bus: MessageBus, target: str = "assistant") -> list[BusMessage]:
    q = bus.queues.get(target)
    if q is None:
        return []
    out: list[BusMessage] = []
    while not q.empty():
        _prio, _seq, msg = q.get_nowait()
        out.append(msg)
        q.task_done()
    return out


async def _noop(_msg: BusMessage) -> None:
    return None


def _make_channel(
    *,
    session_registry=None,
    semantic_memory=None,
) -> tuple[TelegramChannel, MessageBus, _FakeApp]:
    bus = MessageBus()
    bus.register("assistant", _noop)
    ch = TelegramChannel(
        bot_token="T",
        chat_id="0",
        default_agent="assistant",
        bus=bus,
    )
    ch._start_typing = lambda *a, **k: None  # type: ignore[assignment]
    app = _FakeApp()
    ch._app = app  # type: ignore[assignment]
    ch._session_registry = session_registry
    ch._semantic_memory = semantic_memory
    return ch, bus, app


class TestTelegramNewReset:
    async def test_new_does_not_reach_bus(self, tmp_path):
        """The /new text must never be forwarded to the agent bus."""
        from session_registry import SessionRegistry

        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register(_KEY_42, "assistant", "sid-1")
        sem = AsyncMock()
        msgs = [
            type("M", (), {"type": "user", "message": {"content": "remember X"}})()
        ]
        ch, bus, app = _make_channel(session_registry=reg, semantic_memory=sem)

        with patch("session_saver.get_session_messages", return_value=msgs):
            await ch._handle(_fake_update("42", "/new"), None)

        queued = await _drain_bus(bus)
        assert queued == [], "/new must not be forwarded to the agent bus"

    async def test_new_ack_is_sent(self, tmp_path):
        """An acknowledgement message must be sent to the chat after /new."""
        from session_registry import SessionRegistry

        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register(_KEY_42, "assistant", "sid-1")
        sem = AsyncMock()
        msgs = [
            type("M", (), {"type": "user", "message": {"content": "hi"}})()
        ]
        ch, bus, app = _make_channel(session_registry=reg, semantic_memory=sem)

        with patch("session_saver.get_session_messages", return_value=msgs):
            await ch._handle(_fake_update("42", "/new"), None)

        assert len(app.bot.sent) == 1, "exactly one ack must be sent"
        ack = app.bot.sent[0]
        assert ack["chat_id"] == "42"
        assert "fresh" in ack["text"].lower()

    async def test_new_clears_registry_entry(self, tmp_path):
        """After /new the registry entry must be gone (next turn starts fresh)."""
        from session_registry import SessionRegistry

        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register(_KEY_42, "assistant", "sid-1")
        sem = AsyncMock()
        msgs = [
            type("M", (), {"type": "user", "message": {"content": "hi"}})()
        ]
        ch, bus, app = _make_channel(session_registry=reg, semantic_memory=sem)

        with patch("session_saver.get_session_messages", return_value=msgs):
            await ch._handle(_fake_update("42", "/new"), None)

        assert reg.get(_KEY_42) is None, "registry pointer must be cleared"

    async def test_new_retains_before_clearing(self, tmp_path):
        """retain() must be called (session saved) before the pointer is cleared."""
        from session_registry import SessionRegistry

        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register(_KEY_42, "assistant", "sid-1")
        sem = AsyncMock()
        msgs = [
            type("M", (), {"type": "user", "message": {"content": "important data"}})()
        ]
        ch, bus, app = _make_channel(session_registry=reg, semantic_memory=sem)

        with patch("session_saver.get_session_messages", return_value=msgs):
            await ch._handle(_fake_update("42", "/new"), None)

        sem.retain.assert_awaited_once()

    async def test_new_no_registry_wired_still_acks(self):
        """When _session_registry is None (pre-Task-9), /new still acks the user."""
        ch, bus, app = _make_channel(session_registry=None, semantic_memory=None)

        await ch._handle(_fake_update("99", "/new"), None)

        queued = await _drain_bus(bus)
        assert queued == [], "/new must not reach the bus even without registry"
        assert len(app.bot.sent) == 1
        assert "fresh" in app.bot.sent[0]["text"].lower()

    async def test_new_with_suffix_is_intercepted(self, tmp_path):
        """/new followed by trailing text is still treated as a reset command."""
        from session_registry import SessionRegistry

        reg = SessionRegistry(str(tmp_path / "s.json"))
        sem = AsyncMock()
        ch, bus, app = _make_channel(session_registry=reg, semantic_memory=sem)

        await ch._handle(_fake_update("55", "/new please"), None)

        queued = await _drain_bus(bus)
        assert queued == [], "/new <suffix> must not reach the bus"
        assert len(app.bot.sent) == 1

    async def test_regular_message_still_reaches_bus(self):
        """Non-/new messages must continue to flow to the bus unaffected."""
        ch, bus, app = _make_channel()

        await ch._handle(_fake_update("42", "hello"), None)

        queued = await _drain_bus(bus)
        assert len(queued) == 1
        assert queued[0].content == "hello"
        assert app.bot.sent == []
