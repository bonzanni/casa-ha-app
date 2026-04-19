"""Tests for Telegram message splitting.

We import the splitting logic directly to avoid pulling in
python-telegram-bot (not installed locally).
"""

import importlib
import sys
import types
from unittest.mock import MagicMock

# Stub out the telegram package so channels.telegram can be imported
_telegram_stub = types.ModuleType("telegram")
_telegram_stub.Update = MagicMock()
_telegram_constants = types.ModuleType("telegram.constants")
_telegram_constants.ChatAction = MagicMock()
_telegram_stub.constants = _telegram_constants
_telegram_error = types.ModuleType("telegram.error")
_telegram_error.TelegramError = type("TelegramError", (Exception,), {})
_telegram_error.NetworkError = type("NetworkError", (Exception,), {})
_telegram_error.TimedOut = type("TimedOut", (Exception,), {})
_telegram_stub.error = _telegram_error
_telegram_ext = types.ModuleType("telegram.ext")
_telegram_ext.Application = MagicMock()
_telegram_ext.ContextTypes = MagicMock()
_telegram_ext.MessageHandler = MagicMock()
_telegram_ext.filters = MagicMock()
_telegram_stub.ext = _telegram_ext
sys.modules.setdefault("telegram", _telegram_stub)
sys.modules.setdefault("telegram.constants", _telegram_constants)
sys.modules.setdefault("telegram.error", _telegram_error)
sys.modules.setdefault("telegram.ext", _telegram_ext)

from channels.telegram import _split_message, _TG_MAX_LENGTH


class TestSplitMessage:
    def test_short_message_unchanged(self):
        result = _split_message("Hello world")
        assert result == ["Hello world"]

    def test_empty_message(self):
        result = _split_message("")
        assert result == [""]

    def test_exact_limit(self):
        text = "a" * _TG_MAX_LENGTH
        result = _split_message(text)
        assert result == [text]

    def test_splits_at_newline(self):
        # Build a message that's slightly over the limit with a newline
        first_part = "x" * (_TG_MAX_LENGTH - 10)
        second_part = "y" * 20
        text = first_part + "\n" + second_part

        result = _split_message(text)
        assert len(result) == 2
        assert result[0] == first_part
        assert result[1] == second_part

    def test_hard_split_when_no_newline(self):
        text = "a" * (_TG_MAX_LENGTH + 100)
        result = _split_message(text)
        assert len(result) == 2
        assert len(result[0]) == _TG_MAX_LENGTH
        assert len(result[1]) == 100

    def test_multiple_splits(self):
        text = "a" * (_TG_MAX_LENGTH * 3)
        result = _split_message(text)
        assert len(result) == 3

    def test_preserves_content(self):
        lines = [f"Line {i}: " + "x" * 100 for i in range(100)]
        text = "\n".join(lines)
        result = _split_message(text)
        rejoined = "\n".join(result)
        # All original content should be present
        for line in lines:
            assert line in rejoined


# ---------------------------------------------------------------------------
# Helpers + fixtures for _handle cid tests
# ---------------------------------------------------------------------------

import types as _types

import pytest

from bus import BusMessage, MessageBus
from channels.telegram import TelegramChannel

pytestmark = pytest.mark.asyncio


def _fake_update(text: str = "hello") -> object:
    user = _types.SimpleNamespace(first_name="User", id=1)
    message = _types.SimpleNamespace(text=text, message_id=42)
    chat = _types.SimpleNamespace(id="123")
    return _types.SimpleNamespace(
        message=message,
        effective_chat=chat,
        effective_user=user,
    )


async def _noop_handler(_msg: BusMessage) -> None:
    return None


class _FakeApp:
    class bot:
        @staticmethod
        async def send_chat_action(**kwargs):  # noqa: ARG004
            pass

        @staticmethod
        async def send_message(**kwargs):  # noqa: ARG004
            pass


@pytest.fixture
def telegram_channel():
    bus = MessageBus()
    bus.register("assistant", _noop_handler)
    channel = TelegramChannel(
        bot_token="T",
        chat_id="123",
        default_agent="assistant",
        bus=bus,
    )
    channel._start_typing = lambda _chat: None  # type: ignore[assignment]
    channel._app = _FakeApp()  # type: ignore[assignment]
    # Expose bus on the channel so tests can drain it.
    channel._bus = bus  # type: ignore[attr-defined]
    return channel


async def _invoke_handle(channel: TelegramChannel, text: str = "hello") -> None:
    await channel._handle(_fake_update(text), None)


async def _drain(channel) -> list[BusMessage]:
    q = channel._bus.queues.get("assistant")
    if q is None:
        return []
    out: list[BusMessage] = []
    while not q.empty():
        _p, _s, msg = q.get_nowait()
        out.append(msg)
        q.task_done()
    return out


# ---------------------------------------------------------------------------
# TestInheritOrAllocateCid — 5.5 §3.2.4a
# ---------------------------------------------------------------------------


class TestInheritOrAllocateCid:
    """_handle reuses a pre-bound cid (webhook mode via middleware) or
    allocates a fresh one (polling mode, no HTTP ingress)."""

    async def test_inherits_cid_when_var_is_bound(self, telegram_channel):
        from log_cid import cid_var

        token = cid_var.set("fedcba98")
        try:
            await _invoke_handle(telegram_channel, text="hello")
        finally:
            cid_var.reset(token)

        msgs = await _drain(telegram_channel)
        assert msgs, "expected at least one bus message"
        assert msgs[-1].context["cid"] == "fedcba98"

    async def test_allocates_cid_when_var_is_default(self, telegram_channel):
        # cid_var default is "-"
        await _invoke_handle(telegram_channel, text="hi")

        msgs = await _drain(telegram_channel)
        assert msgs, "expected at least one bus message"
        cid = msgs[-1].context["cid"]
        import re
        assert re.match(r"^[0-9a-f]{8}$", cid), cid
        assert cid != "-"
