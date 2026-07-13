"""Test the post_perm_keyboard helper on the Telegram channel.

The helper is what `engagement_permission_relay` calls — it composes
the prompt text + ✅/❌ buttons and calls the underlying telegram
``send_to_topic`` (mocked here)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


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
        assert cd_allow == "v1|permission|rid_abc|0"
        assert cd_deny == "v1|permission|rid_abc|1"

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

    async def test_mcp_tool_name_escaped_in_bold_span(self):
        """M11 (v0.52.0): MCP tool names (mcp__x__y) contain underscore runs
        and hyphens — MarkdownV2-reserved. Unescaped they cause a Telegram
        400 that the relay hook turns into an auto-deny. The bold span must
        carry the escaped name."""
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch._engagement_registry = MagicMock()
        ch._engagement_registry.get = MagicMock(
            return_value=MagicMock(topic_id=42)
        )
        ch.send_to_topic = AsyncMock(return_value=7)

        await ch.post_perm_keyboard(
            engagement_id="x" * 32,
            request_id="rid",
            tool_name="mcp__casa-framework__query_engager",
            tool_input={},
        )
        body = ch.send_to_topic.call_args.args[1]
        assert "*mcp\\_\\_casa\\-framework\\_\\_query\\_engager*" in body
        # The raw, unescaped form must NOT be present.
        assert "*mcp__casa-framework__query_engager*" not in body

    async def test_bash_preview_pre_escaped(self):
        """Backtick + backslash inside the ``` code fence must be escaped
        (pre entities escape ONLY those two); ':' and '.' stay literal."""
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch._engagement_registry = MagicMock()
        ch._engagement_registry.get = MagicMock(
            return_value=MagicMock(topic_id=42)
        )
        ch.send_to_topic = AsyncMock(return_value=7)

        await ch.post_perm_keyboard(
            engagement_id="x" * 32,
            request_id="rid",
            tool_name="Bash",
            tool_input={"command": "echo `date` c:\\tmp"},
        )
        body = ch.send_to_topic.call_args.args[1]
        assert "\\`date\\`" in body        # backtick escaped
        assert "c:\\\\tmp" in body          # backslash escaped (c:\\tmp)
        # ':' is NOT MarkdownV2-reserved and must stay literal.
        assert ":" in body

    async def test_parse_failure_falls_back_to_plain_text(self):
        """Defense-in-depth: a MarkdownV2 send failure retries as plain text
        (unformatted keyboard) rather than propagating to a hook-level
        auto-deny."""
        from channels import telegram as tg_mod
        from telegram.error import TelegramError

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch._engagement_registry = MagicMock()
        ch._engagement_registry.get = MagicMock(
            return_value=MagicMock(topic_id=42)
        )
        calls: list = []

        async def _send(topic_id, text, **kwargs):
            calls.append((text, kwargs))
            if kwargs.get("parse_mode") == "MarkdownV2":
                raise TelegramError("can't parse entities")
            return 7

        ch.send_to_topic = _send

        msg_id = await ch.post_perm_keyboard(
            engagement_id="x" * 32,
            request_id="rid",
            tool_name="Bash",
            tool_input={"command": "curl https://example.com"},
        )
        assert msg_id == 7
        assert len(calls) == 2
        # Second (fallback) call carries no parse_mode and still the keyboard.
        assert "parse_mode" not in calls[1][1]
        assert "reply_markup" in calls[1][1]

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
