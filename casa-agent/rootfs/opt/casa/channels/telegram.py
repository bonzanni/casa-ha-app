"""Telegram channel implementation using python-telegram-bot v20+.

Supports two transport modes and two delivery modes:

Transport:
- **Polling** (default): long-polls Telegram for updates.
- **Webhook**: Telegram pushes updates. Set ``webhook_url``.

Delivery:
- **stream**: send first token immediately, edit message in-place as
  more tokens arrive (~1 s throttle). Feels real-time.
- **block**: wait for the full response before sending (classic).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from bus import BusMessage, MessageBus, MessageType
from channels import Channel
from channels.telegram_supervisor import ReconnectSupervisor
from log_cid import new_cid

logger = logging.getLogger(__name__)

# Telegram typing indicator lasts ~5 s; resend every 4 s to keep it alive.
_TYPING_INTERVAL = 4.0

# Typing 401 backoff: initial delay, multiplier, max delay, max consecutive
# failures before circuit-breaking all typing. (Orthogonal to reconnect —
# spec 5.2 §4.3. Do not fold into the supervisor.)
_TYPING_BACKOFF_INIT = 1.0
_TYPING_BACKOFF_FACTOR = 2.0
_TYPING_BACKOFF_MAX = 60.0
_TYPING_CIRCUIT_BREAK = 10

# Stream edit throttle (seconds between editMessageText calls).
_STREAM_THROTTLE = 1.0

# Reconnect supervisor backoff schedule (spec 5.2 §4.2): 1s, 2s, 4s, 8s,
# 16s, cap 60s, unbounded. Reuses retry.compute_backoff_ms via
# ReconnectSupervisor. Module-level so tests can monkey-patch short
# values; do not expose as env vars (spec §9.3).
_RECONNECT_INITIAL_MS = 1000
_RECONNECT_CAP_MS = 60_000

# Health probe: every _PROBE_INTERVAL seconds, call bot.get_me() with
# _PROBE_TIMEOUT. Replaces the v0.5.x _POLL_STALL_THRESHOLD placeholder
# (which only refreshed its own timestamp — no real detection).
_PROBE_INTERVAL = 45.0
_PROBE_TIMEOUT = 10.0

# Callback type for on_token streaming
OnTokenCallback = Callable[[str], Awaitable[None]]


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
        delivery_mode: str = "stream",
        webhook_secret: str = "",
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.default_agent = default_agent
        self._bus = bus
        self._webhook_url = webhook_url
        self._webhook_path = webhook_path
        self._delivery_mode = delivery_mode  # "stream" or "block"
        self._webhook_secret = webhook_secret
        self._app: Application | None = None
        self._typing_tasks: dict[str, asyncio.Task] = {}
        # Typing backoff state (shared across all chats) — item-E-orthogonal.
        self._typing_consecutive_failures: int = 0
        self._typing_suspended: bool = False
        # Reconnect supervisor + health probe task.
        self._supervisor: ReconnectSupervisor | None = None
        self._probe_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initial bring-up. Starts the supervisor, runs one rebuild,
        then launches the health probe loop.
        """
        self._supervisor = ReconnectSupervisor(
            rebuild_fn=self._rebuild,
            logger=logger,
            initial_ms=_RECONNECT_INITIAL_MS,
            cap_ms=_RECONNECT_CAP_MS,
        )
        self._supervisor.start()
        try:
            await self._rebuild()
        except (NetworkError, TimedOut) as exc:
            # Transient failure on first bring-up — hand off to supervisor.
            logger.error(
                "Telegram initial bring-up failed (%s); handing off to supervisor",
                exc,
            )
            self._supervisor.trigger(f"initial_bringup: {exc!r}")
        self._probe_task = asyncio.create_task(self._health_probe_loop())

    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()

        if self._probe_task and not self._probe_task.done():
            self._probe_task.cancel()
            try:
                await self._probe_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._supervisor is not None:
            await self._supervisor.stop()
            self._supervisor = None

        await self._teardown_app()
        logger.info("Telegram channel stopped")

    async def _teardown_app(self) -> None:
        """Best-effort teardown of the current Application.

        Called both on `stop()` and as the first step of `_rebuild()`.
        Swallows exceptions — a failing teardown must not block the
        rebuild that follows.
        """
        app = self._app
        if app is None:
            return
        try:
            if self._webhook_url:
                await app.bot.delete_webhook()
            elif app.updater is not None:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("Telegram teardown swallowed exception: %s", exc)
        finally:
            self._app = None

    async def _rebuild(self) -> None:
        """Build (or rebuild) the Application, register handlers, start it.

        Idempotent — if called while an Application exists, it is torn
        down first. Raises on bring-up failure so the supervisor can
        catch and schedule a retry; callers on the happy path do not
        expect this to raise.
        """
        await self._teardown_app()

        app = (
            Application.builder()
            .token(self.bot_token)
            .build()
        )
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle)
        )
        app.add_error_handler(self._on_ptb_error)
        await app.initialize()
        await app.start()

        if self._webhook_url:
            full_url = self._webhook_url.rstrip("/") + self._webhook_path
            kwargs: dict[str, Any] = {"url": full_url}
            if self._webhook_secret:
                kwargs["secret_token"] = self._webhook_secret
            await app.bot.set_webhook(**kwargs)
            logger.info(
                "Telegram started (webhook, delivery=%s, url=%s, secret=%s)",
                self._delivery_mode, full_url,
                "yes" if self._webhook_secret else "no",
            )
        else:
            await app.updater.start_polling()  # type: ignore[union-attr]
            logger.info(
                "Telegram started (polling, delivery=%s, chat_id=%s)",
                self._delivery_mode, self.chat_id,
            )

        # Publish the rebuilt app atomically.
        self._app = app

    # ------------------------------------------------------------------
    # Health probe + PTB error handler (spec 5.2 §4.2)
    # ------------------------------------------------------------------

    async def _health_probe_loop(self) -> None:
        """Periodic liveness probe against the Bot API.

        Every `_PROBE_INTERVAL` seconds, call `bot.get_me()` with a
        `_PROBE_TIMEOUT` ceiling. On success: quiet. On timeout or
        transport exception: trigger the supervisor. Replaces the
        v0.5.x `_poll_stall_watchdog` placeholder.
        """
        try:
            while True:
                await asyncio.sleep(_PROBE_INTERVAL)
                app = self._app
                supervisor = self._supervisor
                if app is None or supervisor is None:
                    continue  # reconnect in progress — supervisor owns it
                try:
                    await asyncio.wait_for(
                        app.bot.get_me(), timeout=_PROBE_TIMEOUT,
                    )
                except asyncio.CancelledError:
                    raise
                except (NetworkError, TimedOut, asyncio.TimeoutError) as exc:
                    supervisor.trigger(f"probe_failed: {exc!r}")
                except Exception as exc:  # noqa: BLE001 — diagnostic only
                    # Non-transport exceptions: log and keep probing.
                    # Supervisor rebuild would not help.
                    logger.debug(
                        "Telegram health probe non-transport error: %s", exc,
                    )
        except asyncio.CancelledError:
            return

    async def _on_ptb_error(
        self,
        update: object,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """PTB error handler. Routes transport errors to the supervisor.

        Registered via `Application.add_error_handler`. Non-transport
        errors (handler bugs, ValueError, etc.) are logged but do NOT
        trigger a reconnect — rebuilding the Application would not fix
        them.
        """
        exc = getattr(context, "error", None)
        if isinstance(exc, (NetworkError, TimedOut)):
            if self._supervisor is not None:
                self._supervisor.trigger(f"ptb_error: {type(exc).__name__}: {exc}")
            return
        if exc is not None:
            logger.warning("Telegram handler error (not retryable): %s", exc)

    async def process_webhook_update(self, payload: dict) -> None:
        """Process a webhook update payload from aiohttp."""
        if self._app is None:
            return
        update = Update.de_json(payload, self._app.bot)
        if update:
            await self._app.process_update(update)

    # ------------------------------------------------------------------
    # Typing indicator (with 401 backoff)
    # ------------------------------------------------------------------

    def _start_typing(self, chat_id: str) -> None:
        """Start a background loop that sends 'typing...' to *chat_id*."""
        if self._typing_suspended:
            return
        existing = self._typing_tasks.get(chat_id)
        if existing and not existing.done():
            return
        self._typing_tasks[chat_id] = asyncio.create_task(
            self._typing_loop(chat_id)
        )

    def _stop_typing(self, chat_id: str) -> None:
        """Cancel the typing indicator for *chat_id*."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(self, chat_id: str) -> None:
        """Send 'typing' chat action until cancelled, with backoff on failure."""
        backoff = _TYPING_BACKOFF_INIT
        try:
            while True:
                if self._typing_suspended or self._app is None:
                    return
                try:
                    await self._app.bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                    # Success -- reset backoff
                    self._typing_consecutive_failures = 0
                    backoff = _TYPING_BACKOFF_INIT
                except TelegramError as exc:
                    self._typing_consecutive_failures += 1
                    if self._typing_consecutive_failures >= _TYPING_CIRCUIT_BREAK:
                        self._typing_suspended = True
                        logger.error(
                            "Typing suspended after %d failures. "
                            "Bot token may be invalid.",
                            self._typing_consecutive_failures,
                        )
                        return
                    logger.warning(
                        "Typing failed (%d/%d): %s — backing off %.1fs",
                        self._typing_consecutive_failures,
                        _TYPING_CIRCUIT_BREAK,
                        exc,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * _TYPING_BACKOFF_FACTOR, _TYPING_BACKOFF_MAX)
                    continue
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
                "cid": new_cid(),
            },
        )
        await self._bus.send(msg)

    # ------------------------------------------------------------------
    # Outbound: block mode
    # ------------------------------------------------------------------

    async def send(self, message: str, context: dict[str, Any]) -> None:
        """Send a complete message (block mode fallback)."""
        if self._app is None:
            logger.warning("Telegram channel not started; cannot send message")
            return

        target_chat = context.get("chat_id", self.chat_id)
        self._stop_typing(str(target_chat))

        for chunk in _split_message(message):
            await self._app.bot.send_message(
                chat_id=target_chat,
                text=chunk,
            )

    # ------------------------------------------------------------------
    # Outbound: streaming
    # ------------------------------------------------------------------

    def create_on_token(self, context: dict[str, Any]) -> OnTokenCallback:
        """Return a callback for streaming tokens to this chat.

        The callback receives the *accumulated* response text on each
        token.  In ``stream`` mode it edits the message in place; in
        ``block`` mode it's a no-op (the final send handles delivery).
        """
        if self._delivery_mode != "stream":
            # Block mode: no-op callback, send() handles everything
            async def _noop(text: str) -> None:
                pass
            return _noop

        target_chat = str(context.get("chat_id", self.chat_id))
        state: dict[str, Any] = {
            "message_id": None,
            "last_edit": 0.0,
        }

        async def _stream_token(accumulated_text: str) -> None:
            if self._app is None:
                return

            now = time.monotonic()

            if state["message_id"] is None:
                # First token: send new message, stop typing
                self._stop_typing(target_chat)
                try:
                    result = await self._app.bot.send_message(
                        chat_id=target_chat,
                        text=accumulated_text,
                    )
                    state["message_id"] = result.message_id
                    state["last_edit"] = now
                except TelegramError as exc:
                    logger.warning("Stream send failed: %s", exc)
            elif now - state["last_edit"] >= _STREAM_THROTTLE:
                # Throttled edit
                if len(accumulated_text) > _TG_MAX_LENGTH:
                    return  # Stop editing, final send will split
                try:
                    await self._app.bot.edit_message_text(
                        chat_id=target_chat,
                        message_id=state["message_id"],
                        text=accumulated_text,
                    )
                    state["last_edit"] = now
                except TelegramError as exc:
                    # "Message is not modified" is fine (identical text)
                    if "not modified" not in str(exc).lower():
                        logger.warning("Stream edit failed: %s", exc)

        return _stream_token

    async def finalize_stream(
        self, full_text: str, context: dict[str, Any], on_token: OnTokenCallback
    ) -> None:
        """Send the final version of a streamed response.

        In stream mode, does a final edit to ensure the complete text
        is displayed.  Falls back to send() if streaming never started
        (e.g., empty response or stream mode was block).
        """
        if self._app is None:
            return

        target_chat = context.get("chat_id", self.chat_id)
        self._stop_typing(str(target_chat))

        if self._delivery_mode != "stream":
            # Block mode: just send
            await self.send(full_text, context)
            return

        # Retrieve state from the closure
        state = getattr(on_token, "__self__", None)
        # For closures we peek at the cell variables
        message_id = None
        if hasattr(on_token, "__closure__") and on_token.__closure__:
            for cell in on_token.__closure__:
                try:
                    val = cell.cell_contents
                    if isinstance(val, dict) and "message_id" in val:
                        message_id = val["message_id"]
                        break
                except ValueError:
                    continue

        if message_id is None:
            # Streaming never sent a message — fall back to regular send
            await self.send(full_text, context)
            return

        # Final edit with complete text
        if len(full_text) <= _TG_MAX_LENGTH:
            try:
                await self._app.bot.edit_message_text(
                    chat_id=target_chat,
                    message_id=message_id,
                    text=full_text,
                )
            except TelegramError as exc:
                if "not modified" not in str(exc).lower():
                    logger.warning("Final stream edit failed: %s", exc)
        else:
            # Response exceeded the limit — edit first chunk, send the rest
            chunks = _split_message(full_text)
            try:
                await self._app.bot.edit_message_text(
                    chat_id=target_chat,
                    message_id=message_id,
                    text=chunks[0],
                )
            except TelegramError:
                pass
            for chunk in chunks[1:]:
                await self._app.bot.send_message(
                    chat_id=target_chat,
                    text=chunk,
                )


_TG_MAX_LENGTH = 4096


def _split_message(text: str) -> list[str]:
    """Split *text* into chunks that fit within Telegram's message limit."""
    if len(text) <= _TG_MAX_LENGTH:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= _TG_MAX_LENGTH:
            chunks.append(text)
            break

        split_at = text.rfind("\n", 0, _TG_MAX_LENGTH)
        if split_at == -1 or split_at == 0:
            split_at = _TG_MAX_LENGTH

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks
