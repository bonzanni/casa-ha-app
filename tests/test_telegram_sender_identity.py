"""#336: per-sender Telegram identity at the channel boundary.

The `telegram_sender` peer strategy used to fix ``user_peer`` to the
configured constant (``nicola``) for every accepted sender. With
``telegram_chat_id`` empty ("accept all chats") that attributed any Telegram
user's turns to the operator AND ran them at the operator's private recall
clearance. `_handle` now resolves the identity per sender and stamps the
reserved origin markers so the recall gate reads the per-sender clearance.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from bus import BusMessage, MessageBus
from channels.telegram import TelegramChannel
from ingress_identity import IngressIdentityError

pytestmark = pytest.mark.asyncio


class _FakeBot:
    async def send_message(self, **kwargs: Any) -> Any:
        return types.SimpleNamespace(message_id=1)


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _FakeBot()


def _fake_update(
    chat_id: str = "42", text: str = "hi", user_id: int | None = 7,
) -> Any:
    user = (
        types.SimpleNamespace(first_name="User", id=user_id)
        if user_id is not None else None
    )
    message = types.SimpleNamespace(text=text, message_id=42)
    chat = types.SimpleNamespace(id=chat_id)
    return types.SimpleNamespace(
        message=message, effective_chat=chat, effective_user=user)


async def _drain(bus: MessageBus, target: str = "assistant") -> list[BusMessage]:
    q = bus.queues.get(target)
    if q is None:
        return []
    out = []
    while not q.empty():
        _p, _s, msg = q.get_nowait()
        out.append(msg)
        q.task_done()
    return out


async def _noop_handler(_msg: BusMessage) -> None:
    return None


def _channel(configured_chat_id: str):
    bus = MessageBus()
    bus.register("assistant", _noop_handler)
    channel = TelegramChannel(
        bot_token="T", chat_id=configured_chat_id,
        default_agent="assistant", bus=bus,
    )
    channel._start_typing = lambda *a, **k: None  # type: ignore[assignment]
    channel._app = _FakeApp()  # type: ignore[assignment]
    return channel, bus


class TestOperatorSender:
    async def test_operator_sender_is_the_operator_peer_at_private(self):
        # In the standard DM setup the configured chat id IS the operator's
        # user id.
        channel, bus = _channel("7")
        await channel._handle(_fake_update(chat_id="7", user_id=7), None)
        (msg,) = await _drain(bus)
        assert msg.trusted_user_origin.user_peer == "nicola"
        assert msg.trusted_user_origin.server_origin.clearance == "private"
        assert msg.context["_origin_route"] == "telegram"
        assert msg.context["_origin_clearance"] == "private"


class TestNonOperatorSender:
    async def test_non_operator_gets_per_sender_peer_at_public(self):
        # Pre-#336 red case: this sender resolved to user_peer "nicola"
        # with private clearance.
        channel, bus = _channel("0")
        await channel._handle(_fake_update(chat_id="42", user_id=7), None)
        (msg,) = await _drain(bus)
        assert msg.trusted_user_origin.user_peer == "telegram:7"
        assert msg.trusted_user_origin.server_origin.clearance == "public"
        assert msg.context["_origin_clearance"] == "public"

    async def test_accept_all_mode_has_no_operator(self):
        # telegram_chat_id empty = accept-all: there is no configured
        # operator identity, so no sender resolves to the operator peer.
        channel, bus = _channel("")
        await channel._handle(_fake_update(chat_id="7", user_id=7), None)
        (msg,) = await _drain(bus)
        assert msg.trusted_user_origin.user_peer == "telegram:7"
        assert msg.trusted_user_origin.server_origin.clearance == "public"

    async def test_sender_less_update_fails_loudly_not_as_operator(self):
        # An anonymous group/channel post (no effective_user) used to be
        # silently attributed to the operator; #203 doctrine says an
        # unattributable ingress turn dies loudly instead.
        channel, bus = _channel("")
        with pytest.raises(IngressIdentityError):
            await channel._handle(_fake_update(chat_id="42", user_id=None), None)
        assert await _drain(bus) == []


class TestOperatorDetermination:
    def test_group_configured_chat_never_names_an_operator(self):
        # A supergroup id is negative and can never equal a user id — group
        # members are not the operator.
        channel, _ = _channel("-100123")
        assert not channel._sender_is_operator(
            types.SimpleNamespace(id=100123))

    def test_operator_match_is_exact(self):
        channel, _ = _channel("7")
        assert channel._sender_is_operator(types.SimpleNamespace(id=7))
        assert not channel._sender_is_operator(types.SimpleNamespace(id=77))
        assert not channel._sender_is_operator(None)
