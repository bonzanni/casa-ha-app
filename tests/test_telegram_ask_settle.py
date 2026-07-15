"""v0.79.0 (§4) — production-faithful settle-edit regression (the real S1).

``edit_topic_message(clear_keyboard=True)`` and ``edit_perm_keyboard_outcome``
must send an EXPLICIT EMPTY ``InlineKeyboardMarkup([])`` — a bare
``edit_message_text``/``reply_markup=None`` leaves the buttons tappable (PTB
drops None params), which was the settle-path bug: answered/expired questions
stayed re-tappable and only gave a toast.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardMarkup

pytestmark = pytest.mark.asyncio

SUPERGROUP = -1002


def _mk_channel():
    from channels.telegram import TelegramChannel

    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    ch = TelegramChannel(
        bot=bot, chat_id=100, engagement_supergroup_id=SUPERGROUP,
    )
    return ch, bot


async def test_edit_topic_message_clear_keyboard_sends_present_empty_markup():
    ch, bot = _mk_channel()
    ok = await ch.edit_topic_message(
        999, 7001, "Q1: Proceed?\n✅ B", clear_keyboard=True,
    )
    assert ok is True
    _, kwargs = bot.edit_message_text.call_args
    # reply_markup is PRESENT (not dropped) and EMPTY (buttons gone).
    assert "reply_markup" in kwargs
    markup = kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert list(markup.inline_keyboard) == []
    assert kwargs["text"] == "Q1: Proceed?\n✅ B"


async def test_edit_topic_message_without_clear_keeps_bare_edit():
    ch, bot = _mk_channel()
    await ch.edit_topic_message(999, 7002, "plain text")
    _, kwargs = bot.edit_message_text.call_args
    # Legacy callers (no settle) must NOT touch the markup.
    assert "reply_markup" not in kwargs


async def test_perm_settle_sends_present_empty_markup():
    ch, bot = _mk_channel()
    await ch.edit_perm_keyboard_outcome(
        topic_id=999, message_id=8001, outcome={"outcome": "answered"},
    )
    _, kwargs = bot.edit_message_reply_markup.call_args
    assert "reply_markup" in kwargs
    markup = kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert list(markup.inline_keyboard) == []
