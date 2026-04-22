"""Tests for Telegram routing to engagement topics."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _mk_update(*, chat_id, text, thread_id=None, user_id=77):
    u = MagicMock()
    u.message = MagicMock()
    u.message.chat = MagicMock()
    u.message.chat.id = chat_id
    u.message.text = text
    u.message.message_thread_id = thread_id
    u.message.from_user = MagicMock(id=user_id)
    u.message.message_id = 999
    return u


class TestSupergroupRouting:
    async def test_main_feed_ignored_after_redirect_once(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._driver_send_user_turn = AsyncMock()

        u = _mk_update(chat_id=-1001, text="hi", thread_id=None, user_id=7)
        await ch.handle_update(u)
        # First time: redirect posted
        sg = fake_telegram_bot._supergroups[-1001]
        ch._driver_send_user_turn.assert_not_called()

        # Second time: no additional redirect (rate-limited per user per boot)
        await ch.handle_update(u)
        ch._driver_send_user_turn.assert_not_called()

    async def test_topic_message_routed_to_driver(self, fake_telegram_bot, engagement_fixture):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._driver_send_user_turn = AsyncMock()
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record  # topic_id=555, status=active

        u = _mk_update(chat_id=-1001, text="Alex please continue", thread_id=555)
        await ch.handle_update(u)
        ch._driver_send_user_turn.assert_awaited_once()
        args = ch._driver_send_user_turn.await_args.args
        assert args[0].id == rec.id
        assert args[1] == "Alex please continue"

    async def test_ellen_main_chat_unchanged(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._driver_send_user_turn = AsyncMock()
        ch._route_to_ellen = AsyncMock()

        u = _mk_update(chat_id=100, text="hi Ellen")
        await ch.handle_update(u)
        ch._route_to_ellen.assert_awaited_once()
        ch._driver_send_user_turn.assert_not_called()


class TestSlashCommands:
    async def test_slash_cancel_triggers_cancel(self, fake_telegram_bot, engagement_fixture):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._finalize_cancel = AsyncMock()
        rec = engagement_fixture.active_record

        u = _mk_update(chat_id=-1001, text="/cancel", thread_id=rec.topic_id)
        await ch.handle_update(u)
        ch._finalize_cancel.assert_awaited_once()
        assert ch._finalize_cancel.await_args.args[0].id == rec.id

    async def test_slash_complete_triggers_complete(self, fake_telegram_bot, engagement_fixture):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._finalize_complete_user = AsyncMock()
        rec = engagement_fixture.active_record

        u = _mk_update(chat_id=-1001, text="/complete", thread_id=rec.topic_id)
        await ch.handle_update(u)
        ch._finalize_complete_user.assert_awaited_once()

    async def test_slash_silent_sets_observer_flag(self, fake_telegram_bot, engagement_fixture):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        observer = MagicMock()
        observer.silence = MagicMock()
        ch._observer = observer
        rec = engagement_fixture.active_record

        u = _mk_update(chat_id=-1001, text="/silent", thread_id=rec.topic_id)
        await ch.handle_update(u)
        observer.silence.assert_called_once_with(rec.id)
