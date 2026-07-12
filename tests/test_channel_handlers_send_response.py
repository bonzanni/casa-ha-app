"""Task 7 — TelegramChannel.send_response_to_topic renders CC-engagement replies.

The handler wiring (POST /internal/channel/send_to_topic → send_response_to_topic)
is covered by tests/test_channel_internal_handlers.py; here we exercise the real
channel method's entity rendering + original-text fallback.
"""
from __future__ import annotations

import pytest
from telegram import MessageEntity
from telegram.error import BadRequest

from test_telegram_topic_stream import _mk_channel_with_fake_bot

pytestmark = pytest.mark.asyncio


async def test_send_response_to_topic_emits_entities():
    ch, bot = _mk_channel_with_fake_bot()
    msg_id = await ch.send_response_to_topic(42, "**shipped** `v1`")
    assert msg_id == 12345
    kw = bot.send_message.await_args.kwargs
    assert kw["chat_id"] == -1001
    assert kw["message_thread_id"] == 42
    assert kw["text"] == "shipped v1"
    assert {e.type for e in kw["entities"]} == {MessageEntity.BOLD, MessageEntity.CODE}


async def test_send_response_to_topic_plain_delegates():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_response_to_topic(42, "no markup here")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "no markup here"
    assert "entities" not in kw


async def test_send_response_to_topic_badrequest_falls_back_to_original():
    ch, bot = _mk_channel_with_fake_bot()
    bot.send_message.side_effect = [BadRequest("bad"), _stub_msg(9)]
    await ch.send_response_to_topic(42, "**hi**")
    assert bot.send_message.await_count == 2
    last = bot.send_message.await_args_list[-1].kwargs
    assert last["text"] == "**hi**"  # ORIGINAL
    assert "entities" not in last


async def test_send_response_to_topic_killswitch_off_delegates():
    ch, bot = _mk_channel_with_fake_bot()
    ch._rich_text_enabled = False
    await ch.send_response_to_topic(42, "**hi**")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "**hi**"
    assert "entities" not in kw


def _stub_msg(mid):
    from unittest.mock import MagicMock
    return MagicMock(message_id=mid)
