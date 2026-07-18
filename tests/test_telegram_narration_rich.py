"""R2a — rich narration send/edit primitive + relay wiring (render EVERY edit).

Narration relayed into an engagement topic must render markdown (``**bold**``,
``` `code` ```, fenced-pre) as Telegram ``MessageEntity`` spans instead of
leaking literal markers. The render happens at the Telegram primitive
(``send_to_topic_rich`` / ``edit_topic_message_rich``) and the real
``OutputSequencer`` narration path routes through it, so EVERY send/edit
re-renders from the complete current string.
"""

from __future__ import annotations

import pytest
from telegram import InlineKeyboardMarkup, MessageEntity
from telegram.error import BadRequest

from test_telegram_topic_stream import _mk_channel_with_fake_bot

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# send_to_topic_rich — the narration SEND primitive
# --------------------------------------------------------------------------

async def test_send_rich_bold_sends_entities():
    ch, bot = _mk_channel_with_fake_bot()
    mid = await ch.send_to_topic_rich(42, "**hi**")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "hi"
    assert kw["message_thread_id"] == 42
    assert kw["entities"][0].type == MessageEntity.BOLD
    assert mid == 12345


async def test_send_rich_inline_code_sends_entities():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_to_topic_rich(42, "see `config.yaml` now")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "see config.yaml now"
    assert kw["entities"][0].type == MessageEntity.CODE


async def test_send_rich_fenced_pre_sends_entities():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_to_topic_rich(42, "```python\nx = 1\n```")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "x = 1"
    assert kw["entities"][0].type == MessageEntity.PRE


async def test_send_rich_plain_sends_raw_no_entities():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.send_to_topic_rich(42, "just text")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "just text"
    assert "entities" not in kw


async def test_send_rich_badrequest_falls_back_to_plain_original_once():
    ch, bot = _mk_channel_with_fake_bot()
    bot.send_message.side_effect = [BadRequest("bad entity"), bot.send_message.return_value]
    await ch.send_to_topic_rich(42, "**hi**")
    assert bot.send_message.await_count == 2
    last = bot.send_message.await_args_list[-1].kwargs
    assert last["text"] == "**hi**"  # ORIGINAL, verbatim
    assert "entities" not in last


async def test_send_rich_killswitch_off_is_plain():
    ch, bot = _mk_channel_with_fake_bot()
    ch._rich_text_enabled = False
    await ch.send_to_topic_rich(42, "**hi**")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "**hi**"
    assert "entities" not in kw


# --------------------------------------------------------------------------
# edit_topic_message_rich — the narration EDIT primitive
# --------------------------------------------------------------------------

async def test_edit_rich_bold_edits_with_entities():
    ch, bot = _mk_channel_with_fake_bot()
    ok = await ch.edit_topic_message_rich(42, 999, "**hi**")
    assert ok is True
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["message_id"] == 999
    assert kw["text"] == "hi"
    assert kw["entities"][0].type == MessageEntity.BOLD


async def test_edit_rich_plain_edits_raw_no_entities():
    ch, bot = _mk_channel_with_fake_bot()
    ok = await ch.edit_topic_message_rich(42, 999, "just text")
    assert ok is True
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["text"] == "just text"
    assert "entities" not in kw


async def test_edit_rich_badrequest_falls_back_to_plain_original_once():
    ch, bot = _mk_channel_with_fake_bot()
    bot.edit_message_text.side_effect = [BadRequest("bad entity"), None]
    ok = await ch.edit_topic_message_rich(42, 999, "**hi**")
    assert ok is True
    assert bot.edit_message_text.await_count == 2
    last = bot.edit_message_text.await_args_list[-1].kwargs
    assert last["text"] == "**hi**"  # ORIGINAL, verbatim
    assert "entities" not in last


async def test_edit_rich_not_modified_is_success_no_retry():
    ch, bot = _mk_channel_with_fake_bot()
    bot.edit_message_text.side_effect = BadRequest("Bad Request: message is not modified")
    ok = await ch.edit_topic_message_rich(42, 999, "**hi**")
    assert ok is True
    assert bot.edit_message_text.await_count == 1  # not-modified ⇒ no plain retry


async def test_edit_rich_clear_keyboard_sends_empty_markup():
    ch, bot = _mk_channel_with_fake_bot()
    await ch.edit_topic_message_rich(42, 999, "**hi**", clear_keyboard=True)
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["entities"][0].type == MessageEntity.BOLD
    markup = kw["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert not markup.inline_keyboard  # explicit EMPTY keyboard clears the buttons


async def test_edit_rich_killswitch_off_is_plain():
    ch, bot = _mk_channel_with_fake_bot()
    ch._rich_text_enabled = False
    await ch.edit_topic_message_rich(42, 999, "**hi**")
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["text"] == "**hi**"
    assert "entities" not in kw


# --------------------------------------------------------------------------
# Relay wiring — REAL OutputSequencer, render on EVERY narration send/edit
# --------------------------------------------------------------------------

def _wire_sequencer(ch, *, engagement_id: str = "e1", topic_id: int = 42):
    """A real OutputSequencer whose narration wire fns route through the rich
    Telegram primitives, mirroring casa_core's ``_send_to_topic`` /
    ``_edit_topic_message`` closures."""
    from channels.output_sequencer import OutputSequencer

    async def send_message(tid, text, reply_to=None):
        return await ch.send_to_topic_rich(tid, text)

    async def edit_message(tid, mid, text):
        return await ch.edit_topic_message_rich(tid, mid, text)

    return OutputSequencer(
        engagement_id=engagement_id,
        topic_id=topic_id,
        send_message=send_message,
        edit_message=edit_message,
    )


async def test_relay_open_narration_renders_entities():
    ch, bot = _mk_channel_with_fake_bot()
    seq = _wire_sequencer(ch)
    mid = await seq.open_narration("**bold**")
    assert mid == 12345
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "bold"
    assert kw["entities"][0].type == MessageEntity.BOLD


async def test_relay_edit_narration_renders_entities():
    ch, bot = _mk_channel_with_fake_bot()
    seq = _wire_sequencer(ch)
    mid = await seq.open_narration("start")
    result = await seq.edit_narration_if_latest(mid, "now `code`")
    assert result == "applied"
    kw = bot.edit_message_text.await_args.kwargs
    assert kw["text"] == "now code"
    assert kw["entities"][0].type == MessageEntity.CODE


async def test_relay_renders_every_edit_not_just_terminal():
    """Each narration edit re-renders from the complete current string, so an
    earlier edit that later gets SEALED still carries entities."""
    ch, bot = _mk_channel_with_fake_bot()
    seq = _wire_sequencer(ch)
    mid = await seq.open_narration("plain start")
    await seq.edit_narration_if_latest(mid, "**first** span")
    first_kw = bot.edit_message_text.await_args.kwargs
    assert first_kw["text"] == "first span"
    assert first_kw["entities"][0].type == MessageEntity.BOLD
    # A SECOND, distinct edit renders again (render-every-edit, not terminal-only).
    await seq.edit_narration_if_latest(mid, "**first** and `second`")
    second_kw = bot.edit_message_text.await_args.kwargs
    assert second_kw["text"] == "first and second"
    kinds = {e.type for e in second_kw["entities"]}
    assert MessageEntity.BOLD in kinds and MessageEntity.CODE in kinds
