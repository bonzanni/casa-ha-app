"""Tests for Telegram channel engagement-setup helpers."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


class TestOpenEngagementTopic:
    async def test_creates_topic_with_name_and_icon(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        thread_id = await ch.open_engagement_topic(
            name="#[finance] Test task · abc12345",
            icon_emoji="💰",
        )
        sg = fake_telegram_bot._supergroups[-1001]
        assert thread_id in sg.topics
        assert sg.topics[thread_id].name == "#[finance] Test task · abc12345"
        assert sg.topics[thread_id].icon_emoji == "💰"


class TestSendToTopic:
    async def test_send_to_topic_uses_message_thread_id(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        await ch.send_to_topic(thread_id=555, text="hello from Alex")
        sg = fake_telegram_bot._supergroups[-1001]
        assert sg.messages_by_thread[555] == ["hello from Alex"]


class TestCloseTopicIcon:
    async def test_close_topic_icon_closes_and_flips_to_check(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        thread_id = await ch.open_engagement_topic(name="x", icon_emoji="💰")
        await ch.close_topic_with_check(thread_id=thread_id)
        sg = fake_telegram_bot._supergroups[-1001]
        assert sg.topics[thread_id].closed is True
        assert sg.topics[thread_id].icon_emoji == "✅"


class TestEngagementSetup:
    async def test_register_engagement_commands(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        await ch.setup_engagement_features()
        sg = fake_telegram_bot._supergroups[-1001]
        # Three commands registered under the supergroup scope
        scope_key = next(iter(sg.my_commands_by_scope))
        names = [c["command"] for c in sg.my_commands_by_scope[scope_key]]
        assert set(names) == {"cancel", "complete", "silent"}

    async def test_missing_manage_topics_logs_error(self, fake_telegram_bot, caplog):
        from channels.telegram import TelegramChannel

        sg = fake_telegram_bot._require_supergroup(-1001)
        sg.bot_can_manage_topics = False

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        await ch.setup_engagement_features()
        assert ch.engagement_permission_ok is False
        assert any("can_manage_topics" in r.message for r in caplog.records)

    async def test_skip_when_supergroup_not_configured(self, fake_telegram_bot, caplog):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=None)
        await ch.setup_engagement_features()
        # No crash. No commands registered anywhere.
        assert fake_telegram_bot._supergroups == {}
