"""Telegram channel implementation using python-telegram-bot v20+."""

from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from bus import BusMessage, MessageBus, MessageType
from channels import Channel

logger = logging.getLogger(__name__)


class TelegramChannel(Channel):
    """Bidirectional Telegram channel backed by python-telegram-bot."""

    name: str = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        default_agent: str,
        bus: MessageBus,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.default_agent = default_agent
        self._bus = bus
        self._app: Application | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build the application, register handlers, and start polling."""
        self._app = (
            Application.builder()
            .token(self.bot_token)
            .build()
        )
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle)
        )
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()  # type: ignore[union-attr]
        logger.info("Telegram channel started (chat_id=%s)", self.chat_id)

    async def stop(self) -> None:
        """Stop polling and shut down the application."""
        if self._app is not None:
            if self._app.updater is not None:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram channel stopped")

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def _handle(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle an incoming Telegram text message."""
        if update.message is None or update.message.text is None:
            return

        chat_id = str(update.effective_chat.id) if update.effective_chat else self.chat_id
        user = update.effective_user
        user_name = user.first_name if user else "unknown"

        msg = BusMessage(
            type=MessageType.CHANNEL_IN,
            source="telegram",
            target=self.default_agent,
            content=update.message.text,
            channel="telegram",
            context={
                "chat_id": chat_id,
                "user_name": user_name,
                "message_id": str(update.message.message_id),
            },
        )
        await self._bus.send(msg)

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(self, message: str, context: dict[str, Any]) -> None:
        """Send a text message to the Telegram chat.

        Messages longer than 4096 characters are split into multiple
        messages at line boundaries where possible.
        """
        if self._app is None:
            logger.warning("Telegram channel not started; cannot send message")
            return

        target_chat = context.get("chat_id", self.chat_id)
        for chunk in _split_message(message):
            await self._app.bot.send_message(
                chat_id=target_chat,
                text=chunk,
            )


_TG_MAX_LENGTH = 4096


def _split_message(text: str) -> list[str]:
    """Split *text* into chunks that fit within Telegram's message limit.

    Splits at newline boundaries when possible, falls back to hard split.
    """
    if len(text) <= _TG_MAX_LENGTH:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= _TG_MAX_LENGTH:
            chunks.append(text)
            break

        # Try to split at last newline within limit
        split_at = text.rfind("\n", 0, _TG_MAX_LENGTH)
        if split_at == -1 or split_at == 0:
            split_at = _TG_MAX_LENGTH

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks
