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
import os
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
from log_cid import cid_var, new_cid
from rate_limit import RateLimiter

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

# One-liner sent when a chat exceeds TELEGRAM_RATE_PER_MIN. Only the
# FIRST rejection in a streak triggers this reply (spec 5.2 §8.2);
# subsequent rejects drop silently. Phrasing intentionally kept short
# and non-alarmist — the bucket refills within a minute.
_RATE_LIMIT_REPLY = "Slow down — try again in a minute."

# Stream edit throttle (seconds between editMessageText calls).
_STREAM_THROTTLE = 1.0


def _resolve_chat_id(context: dict[str, Any], default: int | str) -> int | str:
    """Pick a deliverable Telegram chat id from ``context``.

    The ``context["chat_id"]`` slot is overloaded — for user-initiated
    messages it carries a real Telegram chat id (signed integer); for
    scheduled triggers and voice scope probes it carries a session-keying
    label like ``"interval:heartbeat"`` or ``"probe-scope"``. The Telegram
    API rejects non-numeric values with ``BadRequest: Chat not found``,
    which used to bubble through ``finalize_stream → send`` and surface
    as a full traceback at the bus dispatcher. Fall back to the channel's
    registered default when the value isn't a valid chat id.
    """
    raw = context.get("chat_id")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return default
    return default

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
        bot_token: str = "",
        chat_id: str = "",
        default_agent: str = "",
        bus: MessageBus | None = None,
        webhook_url: str = "",
        webhook_path: str = "/telegram/update",
        delivery_mode: str = "stream",
        webhook_secret: str = "",
        rate_limiter: RateLimiter | None = None,
        bot: Any = None,
        engagement_supergroup_id: int | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.default_agent = default_agent
        self._bus = bus
        self._webhook_url = webhook_url
        self._webhook_path = webhook_path
        self._delivery_mode = delivery_mode  # "stream" or "block"
        self._webhook_secret = webhook_secret
        # Rate limiter (spec 5.2 §8). None = unlimited; a limiter with
        # capacity=0 also admits every message.
        self._rate_limiter = rate_limiter
        self._app: Application | None = None
        self._typing_tasks: dict[str, asyncio.Task] = {}
        # Typing backoff state (shared across all chats) — item-E-orthogonal.
        self._typing_consecutive_failures: int = 0
        self._typing_suspended: bool = False
        # Reconnect supervisor + health probe task.
        self._supervisor: ReconnectSupervisor | None = None
        self._probe_task: asyncio.Task | None = None
        # Test-injection bot and engagement supergroup.
        self._bot = bot
        self.engagement_supergroup_id = engagement_supergroup_id
        self.engagement_permission_ok = False
        # Injectable collaborators (wired at startup by Task 22; tests assign AsyncMocks).
        self._engagement_registry = None
        self._observer = None
        self._driver_send_user_turn = None
        self._engagement_driver = None
        self._finalize_cancel = None
        self._finalize_complete_user = None
        self._main_feed_redirect_seen: set[int] = set()
        # Per-topic handler lock (Bug 10, v0.14.7). aiohttp dispatches
        # handle_update concurrently per Bot-API update; without this,
        # /cancel racing a regular turn could route the turn to a driver
        # that's already been torn down by _finalize_engagement. Keyed by
        # message_thread_id (1:1 with engagement via _topic_index). Mirrors
        # in_casa_driver._locks. Entries are not pruned — bounded growth,
        # cleared on add-on restart.
        self._engagement_handler_locks: dict[int, asyncio.Lock] = {}
        # Default routing: forward to the existing PTB handler.
        self._route_to_ellen = self._route_to_ellen_default

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

        _bot_api_base = os.environ.get("TELEGRAM_BOT_API_BASE", "").strip()
        builder = Application.builder().token(self.bot_token)
        if _bot_api_base:
            builder = builder.base_url(f"{_bot_api_base}/bot")
            builder = builder.base_file_url(f"{_bot_api_base}/file/bot")
        app = builder.build()
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

    async def _send_rate_limit_reply(self, chat_id: str) -> None:
        """Send the one-shot 'slow down' reply for a rate-limited chat."""
        if self._app is None:
            return
        try:
            await self._app.bot.send_message(
                chat_id=chat_id, text=_RATE_LIMIT_REPLY,
            )
        except Exception as exc:  # noqa: BLE001
            # A 401/503/etc. on the reply itself is not fatal — the user
            # simply doesn't see the notice this time. Do NOT raise;
            # the rest of _handle has already chosen to drop the
            # message.
            logger.debug(
                "rate-limit reply to chat_id=%s failed: %s", chat_id, exc,
            )

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

        # Rate limit BEFORE typing indicator or bus dispatch (spec 5.2 §8).
        if self._rate_limiter is not None and self._rate_limiter.enabled:
            decision = self._rate_limiter.check(chat_id)
            if not decision.allowed:
                if decision.should_notify:
                    logger.info(
                        "Telegram rate limit hit for chat_id=%s; replying with one-shot notice",
                        chat_id,
                    )
                    await self._send_rate_limit_reply(chat_id)
                return

        self._start_typing(chat_id)

        inherited = cid_var.get()
        cid = inherited if inherited != "-" else new_cid()

        msg = BusMessage(
            type=MessageType.CHANNEL_IN,
            source="telegram",
            target=self.default_agent,
            content=update.message.text,
            channel="telegram",
            context={
                "chat_id": chat_id,
                # Bug 8 (v0.14.6): user_id is needed downstream to enforce
                # originator-only /cancel and /complete on engagement topics.
                # Stored as int when present (Telegram user ids are ints).
                "user_id": user.id if user else None,
                "user_name": user_name,
                "message_id": str(update.message.message_id),
                "cid": cid,
            },
        )
        await self._bus.send(msg)

    # ------------------------------------------------------------------
    # Engagement routing (Task 11)
    # ------------------------------------------------------------------

    async def _route_to_ellen_default(self, update) -> None:
        """Default behavior for non-engagement chats: feed into existing _handle."""
        await self._handle(update, None)

    async def handle_update(self, update) -> None:
        """Single-arg entry-point. Routes by chat_id to engagement or Ellen."""
        msg = getattr(update, "message", None)
        if msg is None:
            return
        chat_id = msg.chat.id
        text = (msg.text or "").strip()
        thread_id = getattr(msg, "message_thread_id", None)
        user_id = msg.from_user.id if msg.from_user else None

        # 1) 1:1 chat with Ellen — existing behaviour
        # Note: self.chat_id may be a string or int — coerce for comparison
        if str(chat_id) == str(self.chat_id):
            return await self._route_to_ellen(update)

        # 2) Engagement supergroup
        if self.engagement_supergroup_id and chat_id == self.engagement_supergroup_id:
            if not thread_id:
                await self._maybe_redirect_main_feed(user_id)
                return
            if self._engagement_registry is None:
                return  # not wired yet; ignore silently
            # Bug 10 (v0.14.7): per-topic lock serialises updates landing
            # in the same engagement so a /cancel can't finish tearing
            # down the driver while a concurrent regular turn is still
            # mid-routing. Different topics still run in parallel.
            lock = self._engagement_handler_locks.setdefault(
                thread_id, asyncio.Lock(),
            )
            async with lock:
                rec = self._engagement_registry.by_topic_id(thread_id)
                if rec is None or rec.status in ("completed", "cancelled", "error"):
                    await self.send_to_topic(thread_id, "No active engagement in this topic.")
                    return

                if text.startswith("/"):
                    command = text.split()[0].lower()
                    # Bug 8 (v0.14.6): /cancel and /complete are originator-only.
                    # Pre-fix any user in the supergroup could terminate any
                    # engagement; user_id wasn't even checked. /silent stays
                    # open since it's local to the engagement (anyone reading
                    # along can quiet the observer in their topic).
                    if command in ("/cancel", "/complete"):
                        owner_id = rec.origin.get("user_id")
                        if (
                            owner_id is not None
                            and user_id is not None
                            and int(owner_id) != int(user_id)
                        ):
                            await self.send_to_topic(
                                thread_id,
                                f"Only the engagement originator can {command}. "
                                "Ask them, or start your own engagement.",
                            )
                            return
                        if command == "/cancel":
                            return await self._finalize_cancel(rec, reason="user")
                        return await self._finalize_complete_user(rec)
                    if command == "/silent":
                        if self._observer is not None:
                            self._observer.silence(rec.id)
                        await self.send_to_topic(
                            thread_id, "Observer quieted for this engagement.",
                        )
                        return

                # Resume suspended client if needed (in_casa driver only — the
                # claude_code driver has s6 keeping the subprocess alive across
                # Casa restarts, and its engagements never have sdk_session_id).
                if rec.driver != "claude_code" and self._engagement_driver is not None:
                    drv = self._engagement_driver
                    if not drv.is_alive(rec) and rec.sdk_session_id:
                        fail_count = rec.origin.get("_resume_fail_count", 0)
                        try:
                            await drv.resume(rec, rec.sdk_session_id)
                            rec.origin["_resume_fail_count"] = 0
                        except Exception as exc:  # noqa: BLE001
                            fail_count += 1
                            rec.origin["_resume_fail_count"] = fail_count
                            logger.warning(
                                "resume failed (%d/2) for engagement %s: %s",
                                fail_count, rec.id[:8], exc,
                            )
                            if fail_count >= 2:
                                await self._engagement_registry.mark_error(
                                    rec.id, kind="resume_failed", message=str(exc),
                                )
                            await self.send_to_topic(
                                thread_id,
                                f"Could not resume this engagement: {exc}. "
                                f"Start a fresh one if needed.",
                            )
                            return
                    elif not drv.is_alive(rec):
                        # No session to resume — orphan
                        await self._engagement_registry.mark_error(
                            rec.id, kind="orphan_no_session",
                            message="no sdk_session_id to resume with",
                        )
                        await self.send_to_topic(
                            thread_id, "This engagement can't be resumed.",
                        )
                        return

                if self._driver_send_user_turn is not None:
                    await self._driver_send_user_turn(rec, text)
                if self._engagement_registry is not None:
                    import time as _time
                    await self._engagement_registry.update_user_turn(rec.id, _time.time())
                return

        # 3) Other chats — existing enforcement applies
        return await self._route_to_ellen(update)

    async def _maybe_redirect_main_feed(self, user_id: int | None) -> None:
        if user_id is None:
            return
        if user_id in self._main_feed_redirect_seen:
            return
        self._main_feed_redirect_seen.add(user_id)
        try:
            await self.bot.send_message(
                chat_id=self.engagement_supergroup_id,
                text=(
                    "This supergroup is for engagement topics. Start one by "
                    "talking to me in our DM."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("main-feed redirect send failed: %s", exc)

    # ------------------------------------------------------------------
    # Bot accessor (supports test injection via bot= kwarg)
    # ------------------------------------------------------------------

    @property
    def bot(self) -> Any:
        if self._bot is not None:
            return self._bot
        return self._app.bot if self._app is not None else None

    # ------------------------------------------------------------------
    # Engagement topic helpers
    # ------------------------------------------------------------------

    ENGAGEMENT_COMMANDS = [
        ("cancel", "Cancel this engagement and close the topic"),
        ("complete", "Mark this engagement complete (no agent summary)"),
        ("silent", "Stop proactive notifications from Ellen for this engagement"),
    ]

    async def setup_engagement_features(self) -> None:
        """Idempotent startup wiring for forum-supergroup engagements.

        - Verifies the bot has ``can_manage_topics`` in the supergroup.
        - Registers slash commands scoped to that supergroup.
        Safe to call when ``engagement_supergroup_id`` is unset — no-op.
        """
        self.engagement_permission_ok = False
        if not self.engagement_supergroup_id:
            return
        # Bot-permissions check
        try:
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(
                chat_id=self.engagement_supergroup_id,
                user_id=me.id,
            )
            if not getattr(member, "can_manage_topics", False):
                logger.error(
                    "Engagement supergroup %s: bot lacks can_manage_topics; "
                    "engagements disabled",
                    self.engagement_supergroup_id,
                )
                return
            self.engagement_permission_ok = True
        except Exception as exc:
            logger.error(
                "Engagement supergroup %s: bot-permissions check failed: %s",
                self.engagement_supergroup_id, exc,
            )
            return

        # setMyCommands — scoped to the supergroup chat
        from telegram import BotCommand, BotCommandScopeChat  # type: ignore
        commands = [BotCommand(c, d) for c, d in self.ENGAGEMENT_COMMANDS]
        scope = BotCommandScopeChat(chat_id=self.engagement_supergroup_id)
        await self.bot.set_my_commands(commands, scope=scope)
        logger.info(
            "Engagement supergroup %s: commands registered (%s)",
            self.engagement_supergroup_id,
            [c for c, _ in self.ENGAGEMENT_COMMANDS],
        )

    async def open_engagement_topic(
        self, *, name: str, icon_emoji: str | None = None,
    ) -> int:
        """Create a Telegram forum topic in the engagement supergroup.

        Returns the ``message_thread_id``. Raises RuntimeError if the
        supergroup is not configured.
        """
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        topic = await self.bot.create_forum_topic(
            chat_id=self.engagement_supergroup_id,
            name=name,
            icon_custom_emoji_id=icon_emoji,
        )
        return topic.message_thread_id

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        """Post a message into the given forum-supergroup topic."""
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        await self.bot.send_message(
            chat_id=self.engagement_supergroup_id,
            text=text,
            message_thread_id=thread_id,
        )

    async def close_topic_with_check(self, thread_id: int) -> None:
        """Close the topic and flip its icon emoji to ✅."""
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        try:
            await self.bot.edit_forum_topic(
                chat_id=self.engagement_supergroup_id,
                message_thread_id=thread_id,
                icon_custom_emoji_id="✅",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "edit_forum_topic failed for thread=%s: %s",
                thread_id, exc,
            )
        await self.bot.close_forum_topic(
            chat_id=self.engagement_supergroup_id,
            message_thread_id=thread_id,
        )

    # ------------------------------------------------------------------
    # Outbound: block mode
    # ------------------------------------------------------------------

    async def send(self, message: str, context: dict[str, Any]) -> None:
        """Send a complete message (block mode fallback)."""
        if self._app is None:
            logger.warning("Telegram channel not started; cannot send message")
            return

        target_chat = _resolve_chat_id(context, self.chat_id)
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

        target_chat = str(_resolve_chat_id(context, self.chat_id))
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

        target_chat = _resolve_chat_id(context, self.chat_id)
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
