"""TelegramChannel.send_media — per-kind positional dispatch (v0.73.0, spec §3.1)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _mk_channel():
    from channels.telegram import TelegramChannel

    fake_bot = MagicMock()
    for m in ("send_document", "send_photo", "send_audio", "send_voice"):
        setattr(fake_bot, m, AsyncMock(return_value=MagicMock(message_id=7)))
    fake_app = MagicMock()
    fake_app.bot = fake_bot
    ch = TelegramChannel(bot_token="x:y", chat_id=100, default_agent="assistant",
                         engagement_supergroup_id=-1001)
    ch._app = fake_app
    ch._stop_typing = MagicMock()          # avoid touching the typing machinery
    return ch, fake_bot


@pytest.mark.parametrize("kind,method", [
    ("document", "send_document"), ("photo", "send_photo"),
    ("audio", "send_audio"), ("voice", "send_voice"),
])
async def test_send_media_dispatches_per_kind(kind, method):
    ch, bot = _mk_channel()
    await ch.send_media(b"BYTES", kind, "f.ext", {"chat_id": 555}, caption="hi")
    target = getattr(bot, method)
    target.assert_awaited_once()
    for other in ("send_document", "send_photo", "send_audio", "send_voice"):
        if other != method:
            getattr(bot, other).assert_not_awaited()
    args, kwargs = target.await_args
    assert args[0] == 555                     # resolved chat id (1st positional)
    inp = args[1]                             # InputFile (2nd positional)
    assert inp.data == b"BYTES"
    assert inp.filename == "f.ext"
    assert kwargs["caption"] == "hi"


async def test_send_media_uses_default_chat_when_context_lacks_numeric():
    ch, bot = _mk_channel()
    await ch.send_media(b"B", "document", "f.pdf", {"chat_id": "not-numeric"})
    assert bot.send_document.await_args.args[0] == 100   # falls back to self.chat_id


async def test_send_media_raises_when_app_none():
    ch, _ = _mk_channel()
    ch._app = None
    with pytest.raises(RuntimeError):
        await ch.send_media(b"B", "document", "f.pdf", {"chat_id": 5})


async def test_base_channel_send_media_not_implemented():
    from channels import Channel

    class _Bare(Channel):
        name = "bare"
        default_agent = "assistant"

        async def start(self):
            ...

        async def send(self, message, context):
            ...

        async def stop(self):
            ...

    with pytest.raises(NotImplementedError):
        await _Bare().send_media(b"B", "document", "f.pdf", {"chat_id": 5})
