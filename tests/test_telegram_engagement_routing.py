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


async def rec_with_session(registry, rec, session_id):
    await registry.persist_session_id(rec.id, session_id)
    await registry.mark_idle(rec.id)


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


class TestResumeOnTurn:
    async def test_resume_called_when_driver_not_alive(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._driver_send_user_turn = AsyncMock()
        driver = MagicMock()
        driver.is_alive = MagicMock(return_value=False)
        driver.resume = AsyncMock()
        ch._engagement_driver = driver
        rec = engagement_fixture.active_record
        rec.sdk_session_id = "sess-xyz"
        await rec_with_session(engagement_fixture.registry, rec, "sess-xyz")

        u = _mk_update(chat_id=-1001, text="Hi again", thread_id=rec.topic_id)
        await ch.handle_update(u)
        driver.resume.assert_awaited_once_with(rec, "sess-xyz")
        ch._driver_send_user_turn.assert_awaited_once()

    async def test_two_resume_failures_mark_error(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._driver_send_user_turn = AsyncMock()
        driver = MagicMock()
        driver.is_alive = MagicMock(return_value=False)
        driver.resume = AsyncMock(side_effect=RuntimeError("rotated"))
        ch._engagement_driver = driver
        rec = engagement_fixture.active_record
        await rec_with_session(engagement_fixture.registry, rec, "sess-xyz")

        u = _mk_update(chat_id=-1001, text="turn1", thread_id=rec.topic_id)
        await ch.handle_update(u)
        u = _mk_update(chat_id=-1001, text="turn2", thread_id=rec.topic_id)
        await ch.handle_update(u)
        assert rec.status == "error"


class TestDriverSendUserTurnRouting:
    """Unit tests for the _driver_send_user_turn closure set by casa_core.

    The closure branches on rec.driver — claude_code goes to the claude_code
    driver, everything else to the in_casa engagement driver.
    """

    async def test_claude_code_rec_routes_to_claude_code_driver(self):
        """When rec.driver == 'claude_code', the closure calls claude_code_driver."""
        from engagement_registry import EngagementRecord

        engagement_driver = MagicMock()
        engagement_driver.send_user_turn = AsyncMock()
        claude_code_driver = MagicMock()
        claude_code_driver.send_user_turn = AsyncMock()

        # Reproduce the closure from casa_core.py E6 change.
        async def _driver_send_user_turn(rec, text):
            if rec.driver == "claude_code":
                await claude_code_driver.send_user_turn(rec, text)
            else:
                await engagement_driver.send_user_turn(rec, text)

        rec = EngagementRecord(
            id="e-cc", kind="executor", role_or_type="configurator",
            driver="claude_code", status="active", topic_id=555,
            started_at=1.0, last_user_turn_ts=2.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None,
            origin={"channel": "telegram", "chat_id": "42"},
            task="do stuff",
        )

        await _driver_send_user_turn(rec, "hello")

        claude_code_driver.send_user_turn.assert_awaited_once_with(rec, "hello")
        engagement_driver.send_user_turn.assert_not_called()

    async def test_in_casa_rec_routes_to_engagement_driver(self):
        """When rec.driver == 'in_casa', the closure calls engagement_driver."""
        from engagement_registry import EngagementRecord

        engagement_driver = MagicMock()
        engagement_driver.send_user_turn = AsyncMock()
        claude_code_driver = MagicMock()
        claude_code_driver.send_user_turn = AsyncMock()

        async def _driver_send_user_turn(rec, text):
            if rec.driver == "claude_code":
                await claude_code_driver.send_user_turn(rec, text)
            else:
                await engagement_driver.send_user_turn(rec, text)

        rec = EngagementRecord(
            id="e-ic", kind="executor", role_or_type="configurator",
            driver="in_casa", status="active", topic_id=555,
            started_at=1.0, last_user_turn_ts=2.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None,
            origin={"channel": "telegram", "chat_id": "42"},
            task="do stuff",
        )

        await _driver_send_user_turn(rec, "hello")

        engagement_driver.send_user_turn.assert_awaited_once_with(rec, "hello")
        claude_code_driver.send_user_turn.assert_not_called()
