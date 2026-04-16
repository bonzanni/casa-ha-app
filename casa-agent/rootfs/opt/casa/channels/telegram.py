"""Telegram channel implementation using python-telegram-bot v20+.

Supports two modes:
- **Polling** (default): the bot long-polls Telegram for updates.
- **Webhook**: Telegram pushes updates to a URL you provide. Set
  ``webhook_url`` to enable. Lower latency, zero idle requests.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from bus import BusMessage, MessageBus, MessageType
from channels import Channel

logger = logging.getLogger(__name__)

# Telegram typing indicator lasts ~5 s; resend every 4 s to keep it alive.
_TYPING_INTERVAL = 4


class TelegramChannel(Channel):
    """Bidirectional Telegram channel backed by python-telegram-bot."""

    name: str = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        default_agent: str,
        bus: MessageBus,
        webhook_url: str = "",
        webhook_path: str = "/telegram/update",
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.default_agent = default_agent
        self._bus = bus
        self._webhook_url = webhook_url
        self._webhook_path = webhook_path
        self._app: Application | None = None
        self._typing_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build the application, register handlers, and start."""
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

        if self._webhook_url:
            full_url = self._webhook_url.rstrip("/") + self._webhook_path
            await self._app.bot.set_webhook(url=full_url)
            logger.info(
                "Telegram channel started in webhook mode (url=%s)", full_url
            )
        else:
            await self._app.updater.start_polling()  # type: ignore[union-attr]
            logger.info(
                "Telegram channel started in polling mode (chat_id=%s)",
                self.chat_id,
            )

    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        # Cancel all typing indicators
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()

        if self._app is not None:
            if self._webhook_url:
                await self._app.bot.delete_webhook()
            elif self._app.updater is not None:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram channel stopped")

    async def process_webhook_update(self, payload: dict) -> None:
        """Process a webhook update payload from aiohttp.

        Called by the aiohttp route handler when Telegram pushes an update.
        """
        if self._app is None:
            return
        update = Update.de_json(payload, self._app.bot)
        if update:
            await self._app.process_update(update)

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    def _start_typing(self, chat_id: str) -> None:
        """Start a background loop that sends 'typing...' to *chat_id*."""
        existing = self._typing_tasks.get(chat_id)
        if existing and not existing.done():
            return  # already typing for this chat
        self._typing_tasks[chat_id] = asyncio.create_task(
            self._typing_loop(chat_id)
        )

    def _stop_typing(self, chat_id: str) -> None:
        """Cancel the typing indicator for *chat_id*."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(self, chat_id: str) -> None:
        """Send 'typing' chat action every few seconds until cancelled."""
        try:
            while True:
                if self._app:
                    await self._app.bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                await asyncio.sleep(_TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass

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

        # Show typing indicator while the agent processes
        self._start_typing(chat_id)

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

        # Stop typing -- we have the response
        self._stop_typing(str(target_chat))

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
