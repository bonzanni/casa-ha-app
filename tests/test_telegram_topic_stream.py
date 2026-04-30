"""Tests for TopicStreamHandle — per-AssistantMessage streaming to engagement topics (Phase 3b / Bug 1)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import TelegramError

pytestmark = pytest.mark.asyncio


def _mk_channel_with_fake_bot(supergroup_id: int = -1001):
    """Build a TelegramChannel with a fake _app.bot for testing."""
    from channels.telegram import TelegramChannel

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(
        return_value=MagicMock(message_id=12345)
    )
    fake_bot.edit_message_text = AsyncMock()

    fake_app = MagicMock()
    fake_app.bot = fake_bot

    ch = TelegramChannel(
        bot_token="x:y",
        chat_id=100,
        default_agent="assistant",
        engagement_supergroup_id=supergroup_id,
    )
    ch._app = fake_app
    return ch, fake_bot


class TestTopicStreamFirstEmit:
    async def test_first_emit_sends_new_message_and_stores_id(self):
        ch, bot = _mk_channel_with_fake_bot()
        handle = ch.create_topic_stream(topic_id=42)

        await handle.emit("hello world")

        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == -1001
        assert kwargs["message_thread_id"] == 42
        assert kwargs["text"] == "hello world"
        assert handle._message_id == 12345


class TestTopicStreamThrottle:
    async def test_subsequent_emit_within_throttle_skipped(self, monkeypatch):
        ch, bot = _mk_channel_with_fake_bot()
        clock = [1000.0]
        monkeypatch.setattr(
            "channels.telegram.time.monotonic",
            lambda: clock[0],
        )

        handle = ch.create_topic_stream(topic_id=42)
        await handle.emit("first")
        clock[0] += 0.5  # less than _STREAM_THROTTLE (1.0)
        await handle.emit("first second")

        bot.send_message.assert_awaited_once()
        bot.edit_message_text.assert_not_awaited()

    async def test_subsequent_emit_after_throttle_window_edits(self, monkeypatch):
        ch, bot = _mk_channel_with_fake_bot()
        clock = [1000.0]
        monkeypatch.setattr(
            "channels.telegram.time.monotonic",
            lambda: clock[0],
        )

        handle = ch.create_topic_stream(topic_id=42)
        await handle.emit("first")
        clock[0] += 1.5  # > _STREAM_THROTTLE
        await handle.emit("first second")

        bot.send_message.assert_awaited_once()
        bot.edit_message_text.assert_awaited_once()
        kwargs = bot.edit_message_text.await_args.kwargs
        assert kwargs["chat_id"] == -1001
        assert kwargs["message_id"] == 12345
        assert kwargs["text"] == "first second"


class TestTopicStreamFinalize:
    async def test_finalize_after_emit_edits_with_full_text(self):
        ch, bot = _mk_channel_with_fake_bot()
        handle = ch.create_topic_stream(topic_id=42)

        await handle.emit("partial")
        await handle.finalize("partial complete final")

        bot.send_message.assert_awaited_once()  # the emit
        bot.edit_message_text.assert_awaited_once()
        assert bot.edit_message_text.await_args.kwargs["text"] == "partial complete final"

    async def test_finalize_without_prior_emit_sends_message(self):
        ch, bot = _mk_channel_with_fake_bot()
        handle = ch.create_topic_stream(topic_id=42)

        await handle.finalize("only this text")

        bot.send_message.assert_awaited_once()
        bot.edit_message_text.assert_not_awaited()
        assert bot.send_message.await_args.kwargs["text"] == "only this text"

    async def test_finalize_overflow_splits_into_multiple_messages(self):
        ch, bot = _mk_channel_with_fake_bot()
        handle = ch.create_topic_stream(topic_id=42)

        await handle.emit("short")
        big_text = "X" * 5000
        await handle.finalize(big_text)

        bot.edit_message_text.assert_awaited()
        # 1 from initial emit + ≥1 overflow chunk(s)
        assert bot.send_message.await_count >= 2


class TestTopicStreamErrorHandling:
    async def test_emit_swallows_not_modified_error(self, monkeypatch):
        ch, bot = _mk_channel_with_fake_bot()
        bot.edit_message_text.side_effect = TelegramError(
            "Bad Request: message is not modified"
        )

        clock = [1000.0]
        monkeypatch.setattr(
            "channels.telegram.time.monotonic",
            lambda: clock[0],
        )

        handle = ch.create_topic_stream(topic_id=42)
        await handle.emit("hi")
        clock[0] += 1.5
        # Should not raise
        await handle.emit("hi")

    async def test_emit_logs_other_errors(self, caplog):
        import logging
        ch, bot = _mk_channel_with_fake_bot()
        bot.send_message.side_effect = TelegramError("network down")

        handle = ch.create_topic_stream(topic_id=42)
        with caplog.at_level(logging.WARNING, logger="channels.telegram"):
            await handle.emit("hello")

        assert any("Stream" in rec.message for rec in caplog.records), (
            f"expected a 'Stream' warning, got: {[r.message for r in caplog.records]}"
        )
