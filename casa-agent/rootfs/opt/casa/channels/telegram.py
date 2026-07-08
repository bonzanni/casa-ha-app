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
import collections
import logging
import os
import time
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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

# M11 (v0.52.0): MarkdownV2 escaping for permission-relay text. Telegram
# rejects a message with parse_mode="MarkdownV2" if reserved chars in the
# dynamic text are unescaped ("can't parse entities", HTTP 400) — and the
# engagement_permission_relay hook treats that failure as an auto-DENY. We
# escape locally (rather than importing telegram.helpers.escape_markdown) so
# the code is stub-independent under tests; the char sets match PTB 22.7's
# escape_markdown(version=2). Outside a code entity every reserved char must
# be backslash-escaped; inside a ``` / ` (pre/code) entity only backtick and
# backslash are special.
_MDV2_SPECIALS = frozenset("_*[]()~`>#+-=|{}.!\\")
_MDV2_PRE_SPECIALS = frozenset("`\\")


def _escape_mdv2(text: str) -> str:
    """Escape MarkdownV2 reserved characters in general (non-code) text."""
    return "".join("\\" + c if c in _MDV2_SPECIALS else c for c in text)


def _escape_mdv2_pre(text: str) -> str:
    """Escape MarkdownV2 reserved characters inside a pre/code entity."""
    return "".join("\\" + c if c in _MDV2_PRE_SPECIALS else c for c in text)


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
        # M10 (v0.52.0): the bot's own @username, cached at setup so
        # handle_update can strip a trailing "@botname" off engagement
        # commands (group command menus send "/cancel@casabot"). None until
        # setup_engagement_features resolves it; while None the suffix is
        # stripped unconditionally (safe inside Casa's own supergroup).
        self._bot_username: str | None = None
        # H5 (v0.52.0): bounded LRU of recently-seen webhook update_ids so a
        # Telegram redelivery (retry after a slow ACK) is processed at most
        # once. OrderedDict used as an insertion-ordered ring buffer.
        self._seen_update_ids: "collections.OrderedDict[int, None]" = (
            collections.OrderedDict()
        )
        # M9 (v0.52.0): strong refs to in-flight engagement-turn delivery
        # tasks. handle_update spawns the SDK turn in the background (so the
        # per-topic lock is not held across a multi-minute turn and /cancel
        # can interrupt it); each task keeps a ref here + a done-callback that
        # discards it. Cancelled in stop().
        self._turn_tasks: set[asyncio.Task] = set()
        # Injectable collaborators (wired at startup by Task 22; tests assign AsyncMocks).
        self._engagement_registry = None
        self._observer = None
        self._driver_send_user_turn = None
        self._engagement_driver = None
        self._finalize_cancel = None
        self._finalize_complete_user = None
        self._session_registry = None   # wired in main() (Task 9); /new reset
        self._semantic_memory = None    # wired in main() (Task 9); /new reset
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

        # M9: cancel any in-flight engagement-turn delivery tasks.
        for task in list(self._turn_tasks):
            task.cancel()
        self._turn_tasks.clear()

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
        # M8 (v0.52.0): each teardown step runs independently. A failing
        # first step (e.g. delete_webhook raising during the very network
        # outage that triggered the rebuild) must NOT skip app.stop()/
        # shutdown() — otherwise the started Application's _update_fetcher
        # task, JobQueue, and HTTPX pools leak on every reload. `except
        # Exception` deliberately does NOT catch asyncio.CancelledError
        # (BaseException), so cancellation still propagates.
        try:
            if self._webhook_url:
                try:
                    await app.bot.delete_webhook()
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.debug("Telegram teardown: delete_webhook failed: %s", exc)
            elif app.updater is not None:
                try:
                    await app.updater.stop()
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.debug("Telegram teardown: updater.stop failed: %s", exc)
            try:
                await app.stop()
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.debug("Telegram teardown: app.stop failed: %s", exc)
            try:
                await app.shutdown()
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.debug("Telegram teardown: app.shutdown failed: %s", exc)
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
        # E-13: dispatch to handle_update (engagement-aware router) so
        # that /cancel, /complete, /silent in engagement topics are
        # intercepted. handle_update internally calls _handle for
        # non-engagement chats (Ellen DM via _route_to_ellen).
        #
        # H5 (v0.52.0): block=False dispatches each update via
        # Application.create_task rather than awaiting it inline, so one
        # long engagement turn cannot stall PTB's sequential update fetcher
        # (default max_concurrent_updates=1) — which in polling mode would
        # otherwise freeze Ellen DMs too. Same-topic ordering stays
        # serialized by _engagement_handler_locks; PTB routes task
        # exceptions to the registered error handler.
        app.add_handler(
            MessageHandler(filters.TEXT, self.handle_update, block=False)
        )
        # E-12 (v0.37.0) Task 20: U1 permission verdicts arrive as inline-keyboard
        # callbacks. CallbackQueryHandler with no pattern matches all callback_data;
        # _on_inline_callback parses by prefix and ignores unknown shapes.
        # block=False (H5): a permission-verdict click must never queue
        # behind a long in-flight in_casa turn.
        app.add_handler(
            CallbackQueryHandler(self._on_inline_callback, block=False)
        )
        app.add_error_handler(self._on_ptb_error)
        # M8 (v0.52.0): roll back a half-started Application if any bring-up
        # step raises, so a started-but-unpublished app (fetcher task,
        # JobQueue, HTTPX pools) is not leaked. Re-raise so the supervisor
        # still schedules a retry.
        try:
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
        except Exception:
            # PTB 22.7 ordering: stop() raises if not running (guard with
            # app.running); shutdown() raises while running (only after
            # stop()).
            try:
                if getattr(app, "running", False):
                    await app.stop()
            except Exception as exc:  # noqa: BLE001 — best-effort rollback
                logger.debug("Telegram rebuild rollback: app.stop failed: %s", exc)
            try:
                await app.shutdown()
            except Exception as exc:  # noqa: BLE001 — best-effort rollback
                logger.debug("Telegram rebuild rollback: app.shutdown failed: %s", exc)
            raise

        # Publish the rebuilt app atomically.
        self._app = app

        # L6 (v0.52.0): a successful rebuild proves token+transport are
        # healthy (initialize() performs getMe and raises InvalidToken on a
        # bad token), so heal the typing circuit breaker — a past outage (or
        # a since-fixed token) must not suppress typing for the process
        # lifetime.
        self._typing_suspended = False
        self._typing_consecutive_failures = 0

        # E-F (v0.30.0): engagement-feature setup MUST run after
        # `self._app = app`. Pre-fix, casa_core.py invoked this once at
        # boot — but if that first `_rebuild` raised on `set_webhook`
        # (transient network blip), `self._app` was never set, the boot
        # call to `setup_engagement_features()` saw `self.bot is None`,
        # raised AttributeError on `None.get_me()`, and left
        # `engagement_permission_ok=False` permanently. Every subsequent
        # supervisor-driven `_rebuild` succeeded, but no path re-ran the
        # engagement setup. Tying it to `_rebuild` makes it self-healing
        # on every successful rebuild. Idempotent — line 647 resets the
        # flag at the start of each call.
        try:
            await self.setup_engagement_features()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "setup_engagement_features failed during _rebuild: %s", exc,
            )

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
        """Enqueue a webhook update payload for PTB's fetcher (fast ACK).

        H5 (v0.52.0): pre-fix this awaited ``Application.process_update``,
        which (default block=True handlers) ran the ENTIRE engagement SDK
        turn before the aiohttp route could return 200 — Telegram timed out
        and redelivered the update, duplicating turns. We now push the
        update onto ``Application.update_queue`` (drained by the internal
        fetcher started by ``app.start()`` in both transports — exactly what
        PTB's own webhook server does) so the route returns within
        milliseconds. A bounded update_id LRU drops redeliveries that were
        already in flight before the first ACK landed.
        """
        if self._app is None:
            return
        update = Update.de_json(payload, self._app.bot)
        if not update:
            return
        uid = getattr(update, "update_id", None)
        if uid is not None:
            if uid in self._seen_update_ids:
                logger.info("Dropping duplicate webhook update_id=%s", uid)
                return
            self._seen_update_ids[uid] = None
            while len(self._seen_update_ids) > 256:
                self._seen_update_ids.popitem(last=False)
        await self._app.update_queue.put(update)

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
                except (NetworkError, TimedOut) as exc:
                    # L6 (v0.52.0): transient transport failure — back off but
                    # do NOT count toward the 401 circuit breaker; the
                    # reconnect supervisor owns transport recovery (spec 5.2
                    # §4.2/§4.3). (Order matters: NetworkError/TimedOut
                    # subclass TelegramError, so this clause must precede it.
                    # Note in PTB 22.7 BadRequest subclasses NetworkError, so
                    # a persistent BadRequest no longer trips the breaker —
                    # bounded because _stop_typing cancels the loop at end of
                    # turn.)
                    logger.warning(
                        "Typing transport error: %s — backing off %.1fs",
                        exc, backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * _TYPING_BACKOFF_FACTOR, _TYPING_BACKOFF_MAX)
                    continue
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

        # /new reset — intercept before rate-limiting or bus dispatch (spec §4.2 #2, C2).
        text = (update.message.text or "").strip()
        # M10 (v0.52.0): tolerate a "/new@botname" mention suffix (defensive —
        # /new is a DM command and private clients don't append it, but a
        # forwarded/group-origin update could).
        _first = text.split()[0].lower() if text else ""
        if _first.split("@", 1)[0] == "/new":
            # M29: ack BEFORE the (potentially multi-second) save so the user
            # gets instant feedback. reset_channel stays awaited (NOT
            # backgrounded) — the registry entry must survive until the retain
            # succeeds so the reaper can retry; in default polling mode PTB
            # serializes updates so no follow-up can resume the old session
            # mid-reset.
            if self._app is not None:
                try:
                    await self._app.bot.send_message(
                        chat_id=chat_id,
                        text="Starting fresh — I still remember what matters.",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("/new ack send to chat_id=%s failed: %s", chat_id, exc)
            if self._session_registry is not None and self._semantic_memory is not None:
                from session_registry import build_session_key
                from session_saver import reset_channel
                channel_key = build_session_key("telegram", chat_id)
                await reset_channel(
                    channel_key, self._session_registry, self._semantic_memory,
                    channel="telegram",
                )
            return  # do NOT feed /new to the agent

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

    async def handle_update(
        self, update, _context: ContextTypes.DEFAULT_TYPE | None = None
    ) -> None:
        """PTB MessageHandler entry-point. Routes by chat_id to engagement or Ellen.

        The optional ``_context`` parameter exists so PTB's
        ``Application.process_update`` can call this callback with its
        ``(update, context)`` two-arg convention. Direct callers (and
        the test suite) may invoke with just ``update``. The context
        object itself is unused — the channel reads everything it needs
        from ``update`` and Casa's bus/engagement registry.
        """
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
                    # M10 (v0.52.0): group command menus send "/cancel@botname"
                    # (Telegram appends @botusername to menu-selected commands
                    # in groups). Strip the mention so the command matches. A
                    # command explicitly addressed to a DIFFERENT bot is
                    # blanked (falls through to the user-turn path, matching
                    # PTB's CommandHandler semantics). When our username isn't
                    # cached yet, strip unconditionally — safe inside Casa's
                    # own supergroup.
                    token = text.split()[0].lower()
                    command, _, mention = token.partition("@")
                    if mention and self._bot_username and mention != self._bot_username:
                        command = ""  # addressed to another bot — not ours
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

                # M9 (v0.52.0): deliver the user turn in a tracked background
                # task so the per-topic lock (and, in polling mode, PTB's
                # update fetcher) is NOT held across the whole multi-minute
                # SDK turn — a subsequent /cancel can then acquire the lock
                # and interrupt the in-flight turn. The status re-check above
                # still ran under the lock, preserving the Bug-10 guarantee
                # (a cancel that already finalised the driver blanks the turn
                # here before any task is spawned).
                if self._engagement_registry is not None:
                    import time as _time
                    await self._engagement_registry.update_user_turn(rec.id, _time.time())
                if self._driver_send_user_turn is not None:
                    task = asyncio.create_task(self._deliver_turn_bg(rec, text))
                    self._turn_tasks.add(task)
                    task.add_done_callback(self._turn_tasks.discard)
                return

        # 3) Other chats. When telegram_chat_id is configured it is an
        #    allowlist (DOCS.md: "Telegram chat ID to restrict messages
        #    to. Leave empty to accept all chats.") — drop updates from
        #    any other chat. Empty chat_id = accept all (documented opt-in).
        #    Route 1 (the configured DM) and route 2 (the engagement
        #    supergroup + its topics) already returned above, so reaching
        #    here means this chat is neither.
        if str(self.chat_id or "").strip():
            logger.info(
                "Dropping Telegram update from unauthorized chat_id=%s "
                "(user_id=%s); telegram_chat_id restriction active",
                chat_id, user_id,
            )
            return
        return await self._route_to_ellen(update)

    async def _deliver_turn_bg(self, rec, text: str) -> None:
        """M9 (v0.52.0): run one engagement user-turn to completion.

        Spawned by handle_update as a tracked background task so the
        per-topic lock is released as soon as the turn is dispatched. If a
        concurrent /cancel finalised the driver mid-turn, send_user_turn
        raises (DriverNotAliveError / transport-closed) and we stay quiet —
        that is the expected end of an interrupted turn, not a failure.
        """
        try:
            await self._driver_send_user_turn(rec, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            latest = (
                self._engagement_registry.get(rec.id)
                if self._engagement_registry is not None else None
            )
            if latest is not None and latest.status in (
                "completed", "cancelled", "error",
            ):
                logger.info(
                    "turn delivery ended by finalize for %s: %s",
                    rec.id[:8], exc,
                )
                return
            logger.warning("turn delivery failed for %s: %s", rec.id[:8], exc)
            try:
                await self.send_to_topic(rec.topic_id, f"Turn failed: {exc}")
            except Exception:  # noqa: BLE001 — best-effort user notice
                pass

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
    # E-12 (v0.37.0) Task 20: inline-keyboard callback dispatch
    # ------------------------------------------------------------------

    async def _on_inline_callback(
        self, update: Any, context: Any = None,
    ) -> None:
        """Dispatch inline-keyboard callbacks (U1 perm verdict, U6 URL noop, …).

        Telegram requires ``callback_query.answer()`` regardless of outcome so
        the client-side spinner clears. After that, parse the ``callback_data``
        prefix and route — unknown / malformed prefixes are silently dropped
        (already logged at the trace level).

        Resolves topic_id → engagement_id locally; the POST to
        ``/internal/channel/permission_verdict`` carries ``engagement_id``
        directly so the casa-main handler (Task 21) doesn't need to redo the
        lookup.
        """
        cq = update.callback_query
        try:
            await cq.answer()
        except Exception as exc:  # noqa: BLE001 — defensive, never propagate
            logger.warning("callback_query.answer failed: %s", exc)

        data = (cq.data or "")
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[0] != "perm" or parts[1] not in ("allow", "deny"):
            logger.debug("inline callback dropped (data=%r)", data)
            return
        verdict, request_id = parts[1], parts[2]
        if not request_id:
            return

        thread_id = getattr(cq.message, "message_thread_id", None)
        if thread_id is None or self._engagement_registry is None:
            logger.warning(
                "inline callback dropped: no thread_id or registry "
                "(thread_id=%s)", thread_id,
            )
            return
        rec = self._engagement_registry.by_topic_id(thread_id)
        if rec is None:
            logger.warning(
                "inline callback dropped: unknown topic_id=%s (verdict=%s)",
                thread_id, verdict,
            )
            return

        payload = {
            "request_id": request_id,
            "verdict": verdict,
            "engagement_id": rec.id,
            "operator_id": getattr(cq.from_user, "id", None),
        }
        try:
            await self._internal_post(
                "/internal/channel/permission_verdict", payload,
            )
        except Exception as exc:  # noqa: BLE001 — never propagate into PTB loop
            logger.warning(
                "permission_verdict internal POST failed (engagement=%s rid=%s): %s",
                rec.id[:8], request_id, exc,
            )

    # ------------------------------------------------------------------
    # v0.37.2 (C-1): permission-relay keyboard composer
    # ------------------------------------------------------------------

    async def post_perm_keyboard(
        self,
        *,
        engagement_id: str,
        request_id: str,
        tool_name: str,
        tool_input: dict,
    ) -> int | None:
        """Post the ✅/❌ permission-relay keyboard to an engagement topic.

        Called by ``engagement_permission_relay`` hook (spec §4.4). Resolves
        the topic_id from ``engagement_registry``. ``send_to_topic`` returns
        the message_id (or None on failure); we pass it through.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        rec = self._engagement_registry.get(engagement_id)
        if rec is None or rec.topic_id is None:
            logger.warning(
                "post_perm_keyboard: unknown engagement or no topic_id "
                "(engagement=%s)", engagement_id[:8],
            )
            return None

        # M11 (v0.52.0): escape the dynamic text. tool_name goes inside a
        # *bold* span (general MarkdownV2 escaping); the preview goes inside a
        # ``` code fence (pre/code escaping — only backtick + backslash). The
        # static prefix has no reserved chars. Unescaped, MCP tool names
        # (mcp__x__y — underscore runs) and Bash previews (backticks/
        # backslashes) trigger a Telegram 400 that the relay hook turns into
        # an auto-deny.
        body_lines = [f"Claude wants to use: *{_escape_mdv2(tool_name)}*"]
        preview = self._build_perm_preview(tool_name, tool_input)
        if preview:
            body_lines.append("")
            body_lines.append(f"```\n{_escape_mdv2_pre(preview)}\n```")
        body = "\n".join(body_lines)

        kbd = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                text="✅ Allow",
                callback_data=f"perm:allow:{request_id}",
            ),
            InlineKeyboardButton(
                text="❌ Deny",
                callback_data=f"perm:deny:{request_id}",
            ),
        ]])

        try:
            return await self.send_to_topic(
                rec.topic_id, body,
                reply_markup=kbd,
                parse_mode="MarkdownV2",
            )
        except TelegramError as exc:
            # M11 defense-in-depth: any residual MarkdownV2 parse failure
            # degrades to an unformatted keyboard rather than a hook-level
            # auto-deny. The operator still sees the request and both buttons.
            logger.warning(
                "post_perm_keyboard MarkdownV2 send failed (engagement=%s); "
                "retrying as plain text: %s", engagement_id[:8], exc,
            )
            plain = f"Claude wants to use: {tool_name}"
            if preview:
                plain += f"\n\n{preview}"
            return await self.send_to_topic(
                rec.topic_id, plain, reply_markup=kbd,
            )

    @staticmethod
    def _build_perm_preview(tool_name: str, tool_input: dict) -> str:
        """Render a short, single-line preview of the tool's identifying field."""
        if tool_name == "Bash":
            return str(tool_input.get("command", ""))[:200]
        if tool_name in ("Edit", "Write", "Read", "Glob", "Grep"):
            return str(
                tool_input.get("file_path") or tool_input.get("pattern") or ""
            )[:200]
        if tool_name.startswith("mcp__"):
            return ""  # MCP tool calls — no useful primary field
        return ""

    async def _internal_post(self, path: str, payload: dict) -> dict:
        """Dispatch an internal-handler call from inside casa-main.

        Used by handlers that need to round-trip into the internal-handler
        family that the per-engagement MCP server also uses. Implemented as
        an HTTP call over the casa-main Unix socket for symmetry with the
        engagement-side ``_internal_post`` in ``casa_engagement_channel`` —
        a future optimization could swap to a direct callable dispatch, but
        the wire-level path is the simplest correct shape today.
        """
        import aiohttp
        connector = aiohttp.UnixConnector(path="/run/casa/internal.sock")
        async with aiohttp.ClientSession(connector=connector) as sess:
            async with sess.post(
                f"http://localhost{path}", json=payload,
            ) as resp:
                return await resp.json()

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
            # M10 (v0.52.0): cache the bot's own @username so handle_update
            # can strip "@botname" off menu-selected group commands. The
            # isinstance guard keeps MagicMock get_me() fakes in tests from
            # poisoning the cache (leaves it None → strip unconditionally).
            _u = getattr(me, "username", None)
            if isinstance(_u, str) and _u:
                self._bot_username = _u.lower()
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
            # v0.37.1 D-1: boot-time diagnostic for the topic-icon map.
            # Non-fatal — logged only if any of our IDs rotated out of
            # Telegram's curated set.
            try:
                from channels import topic_icons
                await topic_icons.verify_against_telegram(self.bot)
            except Exception as exc:  # noqa: BLE001 — diagnostic only
                logger.info(
                    "Engagement supergroup %s: topic_icons verifier "
                    "failed (%s); proceeding", self.engagement_supergroup_id, exc,
                )
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
        self, *, name: str, role: str = "",
    ) -> int:
        """Create a Telegram forum topic in the engagement supergroup.

        v0.37.1 D-1: ``role`` resolves to a numeric ``custom_emoji_id``
        via ``channels.topic_icons.icon_id_for_role`` and is sent as
        ``icon_custom_emoji_id``. Unknown roles fall back to the
        default (🤖) icon — the helper never returns None.

        Returns the ``message_thread_id``. Raises ``RuntimeError`` if
        the supergroup is not configured.
        """
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        from channels.topic_icons import icon_id_for_role
        topic = await self.bot.create_forum_topic(
            chat_id=self.engagement_supergroup_id,
            name=name,
            icon_custom_emoji_id=icon_id_for_role(role),
        )
        return topic.message_thread_id

    async def send_to_topic(
        self, thread_id: int, text: str, **kwargs,
    ) -> int:
        """Post a message into the given forum-supergroup topic.

        Returns the Telegram ``message_id`` of the posted message.
        ``kwargs`` is forwarded to ``bot.send_message`` (used by Phase 2+
        callers passing ``reply_markup`` for inline keyboards, etc.).
        """
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        msg = await self.bot.send_message(
            chat_id=self.engagement_supergroup_id,
            text=text,
            message_thread_id=thread_id,
            **kwargs,
        )
        return msg.message_id

    async def update_topic_state(
        self, *, engagement_id: str, new_state: str,
    ) -> None:
        """E-12 (v0.37.0) Task 23: re-render the topic title with ``new_state``.

        Reuses the existing role_or_type + ``task`` stored on the
        ``EngagementRecord``. Idempotent — when ``new_state`` is identical to
        the persisted ``current_state_emoji`` it's a no-op. Unknown
        ``new_state`` values are also no-ops (defensive). Persistence of the
        new emoji happens after a successful edit_forum_topic so a transient
        Telegram failure doesn't desync the record from what's on the wire.
        """
        from channels.state_emoji import (
            STATE_EMOJI, compose_topic_title, concise_task,
        )
        if self._engagement_registry is None:
            return
        rec = self._engagement_registry.get(engagement_id)
        if rec is None or rec.topic_id is None:
            return
        new_emoji = STATE_EMOJI.get(new_state)
        if new_emoji is None or new_emoji == rec.current_state_emoji:
            return

        title = compose_topic_title(
            state=new_state,
            short_task=concise_task(rec.task or ""),
        )
        if not self.engagement_supergroup_id:
            return
        try:
            await self.bot.edit_forum_topic(
                chat_id=self.engagement_supergroup_id,
                message_thread_id=rec.topic_id,
                name=title,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "U3 update_topic_state edit_forum_topic failed (engagement=%s "
                "state=%s): %s", engagement_id[:8], new_state, exc,
            )
            return
        try:
            await self._engagement_registry.set_channel_state(
                engagement_id, current_state_emoji=new_emoji,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "U3 set_channel_state(%s, %s) failed: %s",
                engagement_id[:8], new_emoji, exc,
            )

    async def close_topic(self, thread_id: int) -> None:
        """Close a forum topic (v0.37.1 D-1 rename + simplification).

        Previously named ``close_topic_with_check`` and flipped the
        bubble icon to ``"✅"`` on close. That was a literal-char value
        rather than the required numeric ``custom_emoji_id``, so it
        silently failed and also stripped the leading char from the
        topic name. v0.37.1: bubble stays as the role icon for the
        engagement's whole lifecycle; state lives in the title (set
        by ``update_topic_state`` upstream). Body simplifies to a
        plain ``close_forum_topic``.
        """
        if not self.engagement_supergroup_id:
            raise RuntimeError("engagement supergroup not configured")
        try:
            await self.bot.close_forum_topic(
                chat_id=self.engagement_supergroup_id,
                message_thread_id=thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "close_forum_topic failed for thread=%s: %s",
                thread_id, exc,
            )

    # ------------------------------------------------------------------
    # Outbound: block mode
    # ------------------------------------------------------------------

    def create_topic_stream(self, topic_id: int) -> "TopicStreamHandle":
        """Build a per-turn streaming handle bound to this topic.

        Used by InCasaDriver to emit AssistantMessage chunks
        progressively rather than buffering the whole turn (Bug 1).
        """
        return TopicStreamHandle(self, topic_id)

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

    async def turn_finished(self, context: dict[str, Any]) -> None:
        """L7 (v0.52.0): teardown for turns that end WITHOUT delivery.

        When a turn produces empty / whitespace-only / `<silent/>` output the
        agent never calls send()/finalize_stream(), so the per-chat typing
        loop started in _handle would keep issuing send_chat_action forever
        (permanent 'typing…' plus one Bot API call every 4s). The agent calls
        this hook on the suppressed-turn path to stop it. In block mode the
        streaming first-token teardown never runs, so this is the only stop.
        """
        target_chat = _resolve_chat_id(context, self.chat_id)
        self._stop_typing(str(target_chat))

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


class TopicStreamHandle:
    """Per-engagement-topic streaming primitive used by InCasaDriver.

    Mirrors the (chat_id-keyed) on_token / finalize_stream pattern that
    Ellen uses on direct DMs (lines 739-859) but parameterised by
    topic_id and bound to the engagement supergroup chat. State
    (message_id, last_edit) lives for the duration of one SDK turn —
    each turn opens a fresh Telegram message in the topic.
    """

    def __init__(self, channel: "TelegramChannel", topic_id: int) -> None:
        self._channel = channel
        self._topic_id = topic_id
        self._message_id: int | None = None
        self._last_edit: float = 0.0

    async def emit(self, accumulated_text: str) -> None:
        """Throttled cumulative-text emit. First call sends a new
        message in the topic; subsequent calls edit-in-place,
        throttled at _STREAM_THROTTLE seconds."""
        bot = (
            getattr(self._channel._app, "bot", None)
            if self._channel._app else None
        )
        if bot is None:
            return

        now = time.monotonic()

        if self._message_id is None:
            try:
                result = await bot.send_message(
                    chat_id=self._channel.engagement_supergroup_id,
                    message_thread_id=self._topic_id,
                    text=accumulated_text,
                )
                self._message_id = result.message_id
                self._last_edit = now
            except TelegramError as exc:
                logger.warning("Stream send failed: %s", exc)
            return

        if now - self._last_edit < _STREAM_THROTTLE:
            return

        if len(accumulated_text) > _TG_MAX_LENGTH:
            return

        try:
            await bot.edit_message_text(
                chat_id=self._channel.engagement_supergroup_id,
                message_id=self._message_id,
                text=accumulated_text,
            )
            self._last_edit = now
        except TelegramError as exc:
            if "not modified" not in str(exc).lower():
                logger.warning("Stream edit failed: %s", exc)

    async def finalize(self, full_text: str) -> None:
        """Final edit (or send) once the SDK loop has drained. Handles
        overflow by editing the first chunk and sending subsequent
        chunks as fresh topic messages."""
        bot = (
            getattr(self._channel._app, "bot", None)
            if self._channel._app else None
        )
        if bot is None:
            return

        if self._message_id is None:
            for chunk in _split_message(full_text):
                try:
                    await bot.send_message(
                        chat_id=self._channel.engagement_supergroup_id,
                        message_thread_id=self._topic_id,
                        text=chunk,
                    )
                except TelegramError as exc:
                    logger.warning("Stream finalize send failed: %s", exc)
            return

        if len(full_text) <= _TG_MAX_LENGTH:
            try:
                await bot.edit_message_text(
                    chat_id=self._channel.engagement_supergroup_id,
                    message_id=self._message_id,
                    text=full_text,
                )
            except TelegramError as exc:
                if "not modified" not in str(exc).lower():
                    logger.warning("Stream finalize edit failed: %s", exc)
            return

        chunks = _split_message(full_text)
        try:
            await bot.edit_message_text(
                chat_id=self._channel.engagement_supergroup_id,
                message_id=self._message_id,
                text=chunks[0],
            )
        except TelegramError as exc:
            if "not modified" not in str(exc).lower():
                logger.warning(
                    "Stream finalize overflow edit failed: %s", exc,
                )
        for chunk in chunks[1:]:
            try:
                await bot.send_message(
                    chat_id=self._channel.engagement_supergroup_id,
                    message_thread_id=self._topic_id,
                    text=chunk,
                )
            except TelegramError as exc:
                logger.warning(
                    "Stream finalize overflow send failed: %s", exc,
                )


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
