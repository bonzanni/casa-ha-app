"""Test the post_perm_keyboard helper on the Telegram channel.

The helper is what `engagement_permission_relay` calls — it composes
the prompt text + ✅/❌ buttons and calls the underlying telegram
``send_to_topic`` (mocked here)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


class TestPostPermKeyboard:
    async def test_composes_buttons_and_posts(self):
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch._engagement_registry = MagicMock()
        ch._engagement_registry.get = MagicMock(
            return_value=MagicMock(topic_id=42)
        )
        ch.send_to_topic = AsyncMock(return_value=99)

        msg_id = await ch.post_perm_keyboard(
            engagement_id="x" * 32,
            request_id="rid_abc",
            tool_name="Bash",
            tool_input={"command": "curl https://example.com"},
        )
        assert msg_id == 99
        ch.send_to_topic.assert_awaited_once()
        args, kwargs = ch.send_to_topic.call_args
        # Topic id from registry lookup.
        assert args[0] == 42
        # Body mentions tool name + a preview of the command.
        body = args[1]
        assert "Bash" in body
        assert "curl" in body
        # Inline keyboard has exactly two buttons with the correct callback_data.
        kbd = kwargs["reply_markup"]
        rows = kbd.inline_keyboard
        assert len(rows) == 1 and len(rows[0]) == 2
        cd_allow = rows[0][0].callback_data
        cd_deny = rows[0][1].callback_data
        assert cd_allow == "perm:allow:rid_abc"
        assert cd_deny == "perm:deny:rid_abc"

    async def test_unknown_engagement_returns_none(self):
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch._engagement_registry = MagicMock()
        ch._engagement_registry.get = MagicMock(return_value=None)
        ch.send_to_topic = AsyncMock()

        msg_id = await ch.post_perm_keyboard(
            engagement_id="z" * 32,
            request_id="r",
            tool_name="Bash",
            tool_input={"command": "x"},
        )
        assert msg_id is None
        ch.send_to_topic.assert_not_called()

    async def test_engagement_without_topic_id_returns_none(self):
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch._engagement_registry = MagicMock()
        ch._engagement_registry.get = MagicMock(
            return_value=MagicMock(topic_id=None)
        )
        ch.send_to_topic = AsyncMock()

        msg_id = await ch.post_perm_keyboard(
            engagement_id="y" * 32,
            request_id="r",
            tool_name="Bash",
            tool_input={"command": "x"},
        )
        assert msg_id is None
        ch.send_to_topic.assert_not_called()
