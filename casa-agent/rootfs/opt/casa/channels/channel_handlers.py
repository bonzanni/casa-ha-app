"""HTTP handlers for /internal/channel/* — POSTed by casa_engagement_channel
over the casa-main Unix socket. Phase 1 exposes /internal/channel/send_to_topic
only; later phases extend the dict returned by ``_make_channel_handlers``.

Body shape: ``{engagement_id: str, ...fields per handler}``.

Response shape:
- success: ``{"ok": True, "message_id": int}``
- known failure: ``{"ok": False, "error": <code>}``

Error codes (Phase 1):
- ``bad_json`` — request body was not valid JSON / not a dict
- ``missing_engagement_id`` — body missing/falsy ``engagement_id``
- ``unknown_engagement`` — registry.get returned None
- ``no_topic_bound`` — engagement record carries no ``topic_id``
- ``send_failed`` — the underlying telegram call raised
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiohttp import web

logger = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# ---------------------------------------------------------------------------
# Module-level state — permission verdict queue (DEPRECATED, v0.75.0)
# ---------------------------------------------------------------------------
#
# v0.75.0 (W5/Sol B3,B4): _make_permission_verdict now delivers straight into
# verdict_broker.BROKER — the long-poll consumer (_make_permission_pending)
# this queue used to feed was removed. _PERMISSION_QUEUES is kept
# accepted-and-ignored for one release (hooks.make_engagement_permission_relay
# still accepts a now-unused ``queues=`` kwarg; _finalize_engagement still
# pops the per-engagement entry as a no-op leak guard) — delete once every
# call site has dropped the parameter.

_PERMISSION_QUEUES: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)

# v0.37.2 (C-1): public alias for consumers outside this module (deprecated
# alongside _PERMISSION_QUEUES above).
PERMISSION_QUEUES = _PERMISSION_QUEUES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_topic(
    engagement_registry: Any, engagement_id: str | None,
) -> tuple[int | None, str | None]:
    """Resolve ``engagement_id`` → ``topic_id`` via the registry.

    Returns ``(topic_id, None)`` on success or ``(None, error_code)`` on
    failure. Error codes: ``missing_engagement_id`` (missing/falsy id),
    ``unknown_engagement`` (registry.get returned None),
    ``no_topic_bound`` (record had no ``topic_id``).
    """
    if not engagement_id:
        return None, "missing_engagement_id"
    rec = engagement_registry.get(engagement_id)
    if rec is None:
        return None, "unknown_engagement"
    topic_id = getattr(rec, "topic_id", None)
    if topic_id is None:
        return None, "no_topic_bound"
    return topic_id, None


# ---------------------------------------------------------------------------
# Handler factories
# ---------------------------------------------------------------------------


def _make_send_to_topic(
    telegram_channel: Any, engagement_registry: Any,
    record_reply: Callable[[str, str], None] | None = None,
) -> Handler:
    """Build the aiohttp POST handler for /internal/channel/send_to_topic.

    ``record_reply`` (W1, optional): every text through this endpoint is a
    ``reply()`` from the engagement; on a successful post it is recorded per
    engagement so the claude_code driver's live topic-stream relay can de-dup
    a streamed turn that is byte-identical to a reply already posted.
    """

    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad_json"})

        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "bad_json"})

        engagement_id = body.get("engagement_id")
        topic_id, err = _resolve_topic(engagement_registry, engagement_id)
        if err is not None:
            return web.json_response({"ok": False, "error": err})

        text = body.get("text") or ""

        request_id = body.get("request_id")
        projection_hash = body.get("projection_hash")
        driver = _resolve_active_driver()

        # The actual post + post-side bookkeeping (advance first-contact, reply
        # de-dup hint). Invoked RELAY-SIDE (§2, review C1) at the reply's
        # tool_use block position — AFTER any preceding narration — or directly
        # here in the no-driver/degraded fallback (pre-v0.79 eager behavior).
        async def _do_post() -> int | None:
            try:
                mid = await telegram_channel.send_response_to_topic(topic_id, text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "send_to_topic failed for engagement=%s topic=%s: %s",
                    engagement_id, topic_id, exc,
                )
                return None
            # W2/Sol B9 (Task 7): the agent's first outbound reply flips
            # first_contact_required -> awaiting_operator. getattr-tolerant.
            advance = getattr(
                engagement_registry, "advance_interaction_state", None)
            if advance is not None:
                await advance(engagement_id, "first_contact")
            if record_reply is not None and text:
                try:
                    record_reply(engagement_id, text)
                except Exception:  # noqa: BLE001 — de-dup hint is best-effort
                    logger.debug("record_reply hook failed", exc_info=True)
            return mid

        # v0.79.0 (§2, review C1): DEFERRED posting. The reply ingress registers
        # + arms a discrete-send INTENT whose poster performs the actual send;
        # the RELAY posts it at the reply's tool_use block (sealing preceding
        # narration first). A response-loss-after-post retry (same request_id)
        # reattaches to the recorded outcome instead of posting a SECOND reply.
        if driver is not None and request_id and projection_hash:
            from channels.output_sequencer import REPLY_TOOL
            res = driver.register_send_intent(
                engagement_id=engagement_id, request_id=request_id,
                tool_name=REPLY_TOOL, projection_hash=projection_hash,
                poster=_noop_poster,
            )
            if res is not None:
                _intent, created_intent = res
                if not created_intent:
                    prior = driver.send_intent_outcome(engagement_id, request_id)
                    if prior is not None and prior.get("message_id") is not None:
                        # Response-loss-after-post retry: the reply already
                        # posted — return its id, no second post (§2(1)).
                        return web.json_response(
                            {"ok": True, "message_id": prior["message_id"]})
                    # F5 fail-closed: reattach BEFORE the relay posted — AWAIT the
                    # same bounded resolution rather than returning ok:true on an
                    # UNRESOLVED intent. A None/timeout/failed outcome maps to
                    # ok:false (never a phantom ok:true with no post).
                    outcome = await _await_deferred_post(
                        driver, engagement_id, request_id)
                    if outcome is not None and outcome.get("message_id") is not None:
                        return web.json_response(
                            {"ok": True, "message_id": outcome["message_id"]})
                    return web.json_response(
                        {"ok": False, "error": "send_failed"})
                driver.set_send_intent_poster(engagement_id, request_id, _do_post)
                driver.arm_send_intent(engagement_id, request_id)
                # F3/F5 fail-closed: AWAIT the relay-mediated post's outcome
                # (bounded by the sequencer's transport budget). An unresolved
                # (None/timeout) or failed outcome is ok:false — never an ok:true
                # with no post.
                outcome = await _await_deferred_post(
                    driver, engagement_id, request_id)
                if outcome is None or not outcome.get("ok"):
                    return web.json_response(
                        {"ok": False, "error": "send_failed"})
                return web.json_response(
                    {"ok": True, "message_id": outcome.get("message_id")})

        # EAGER fallback (no live sequencer): post now and return the id.
        msg_id = await _do_post()
        if msg_id is None:
            return web.json_response({"ok": False, "error": "send_failed"})
        return web.json_response({"ok": True, "message_id": msg_id})

    return handler


def _make_post_inline_keyboard(
    telegram_channel: Any, engagement_registry: Any,
) -> Handler:
    """Build the aiohttp POST handler for /internal/channel/post_inline_keyboard.

    Phase 2 (Task 19): renders an inline-keyboard prompt in the engagement
    topic. The channel server uses this for U1 permission relay (Task 18).

    Body shape: ``{engagement_id, text, buttons: [[{text, callback_data?,
    url?}, ...], ...], parse_mode?, request_id?}``. ``request_id`` is logged
    by the channel server for traceability but ignored by this layer.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad_json"})
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "bad_json"})

        engagement_id = body.get("engagement_id")
        topic_id, err = _resolve_topic(engagement_registry, engagement_id)
        if err is not None:
            return web.json_response({"ok": False, "error": err})

        rows = body.get("buttons") or []
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    text=btn["text"],
                    callback_data=btn.get("callback_data"),
                    url=btn.get("url"),
                )
                for btn in row
            ]
            for row in rows
        ])

        try:
            msg_id = await telegram_channel.send_to_topic(
                topic_id,
                body.get("text") or "",
                reply_markup=keyboard,
                parse_mode=body.get("parse_mode"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "post_inline_keyboard failed for engagement=%s topic=%s: %s",
                engagement_id, topic_id, exc,
            )
            return web.json_response({"ok": False, "error": "send_failed"})

        return web.json_response({"ok": True, "message_id": msg_id})

    return handler


def _make_permission_verdict(engagement_registry: Any) -> Handler:
    """POST /internal/channel/permission_verdict — casa-main → channel server.

    v0.75.0 (W5/Sol B3,B4): CallbackQueryHandler posts here when an operator
    taps the U1 inline-keyboard verdict button. Delivers the verdict directly
    into ``verdict_broker.BROKER`` (namespace ``"permission"``, scope =
    engagement_id) — the ``engagement_permission_relay`` hook awaits that
    same broker request, so this is a pure hand-off with no queue in between.

    Body shape: ``{engagement_id, request_id, verdict, operator_id?}``.
    Response: ``{"ok": True, "result": <broker deliver() outcome>}`` or a
    known failure ``{"ok": False, "error": <code>}``.

    ``result`` is one of ``"delivered"`` (this tap won the live request),
    ``"stale"`` (no live request — timed out/cancelled/already resolved), or
    ``"duplicate"`` (a winning tap already claimed this request).

    Error codes: ``bad_json``, ``missing_engagement_id``, ``missing_request_id``,
    ``missing_verdict``, ``unknown_engagement``.
    """

    async def handler(request: web.Request) -> web.Response:
        from verdict_broker import BROKER

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad_json"})
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "bad_json"})

        eng_id = body.get("engagement_id")
        if not eng_id:
            return web.json_response(
                {"ok": False, "error": "missing_engagement_id"},
            )
        request_id = body.get("request_id")
        if not request_id:
            return web.json_response(
                {"ok": False, "error": "missing_request_id"},
            )
        verdict = body.get("verdict")
        if verdict not in ("allow", "deny"):
            return web.json_response(
                {"ok": False, "error": "missing_verdict"},
            )
        rec = engagement_registry.get(eng_id)
        if rec is None or getattr(rec, "status", None) not in ("active", "idle"):
            return web.json_response(
                {"ok": False, "error": "unknown_engagement"},
            )

        option_index = 0 if verdict == "allow" else 1
        result = BROKER.deliver(
            namespace="permission", scope=eng_id, request_id=request_id,
            option_index=option_index, actor_id=body.get("operator_id"),
        )
        return web.json_response({"ok": True, "result": result})

    return handler


# ---------------------------------------------------------------------------
# v0.75.0 (W5) — engagement_ask: operator-facing multiple-choice question,
# posted by the casa_engagement_channel `ask` MCP tool.
# ---------------------------------------------------------------------------

# Telegram callback_data v1|engagement_ask|<rid>|<idx> caps request_id at the
# same headroom the permission namespace uses (_RID_MAX_LEN in hooks.py); the
# ask tool's request_id is always a full uuid4().hex (32 chars), well under.
_ASK_MIN_OPTIONS = 2
_ASK_MAX_OPTIONS = 8
_ASK_MAX_LABEL_LEN = 48
_ASK_MAX_QUESTION_LEN = 1024
_ASK_MIN_TIMEOUT_S = 30.0
_ASK_MAX_TIMEOUT_S = 570.0
_ASK_DEFAULT_TIMEOUT_S = 300.0

# v0.79.0 §4 — pinned settle copy (appended below the canonical question text
# when the keyboard settles; the keyboard is cleared via clear_keyboard=True).
_SETTLE_ANSWERED = "\n✅ {label}"
_SETTLE_EXPIRED = "\n⌛ expired — answer by text below"
_SETTLE_CANCELLED = "\n🚫 cancelled"
_SETTLE_SUPERSEDED = "\n🚫 superseded by your message below"
# v0.79.0 §4 (Sol F6): the open-question ledger write failed AFTER the keyboard
# posted — settle it fail-closed so no keyboard the boot reconciler can't see
# stays live-tappable.
_SETTLE_INTERNAL_ERROR = "\n⚠️ internal error — question withdrawn, please resend"

# v0.79.0 §4 — free-text anchor: an ``options: []`` ask posts a numbered anchor
# with NO keyboard; the next operator text settles it.
_SETTLE_ANSWERED_BELOW = "\n✅ answered below"

# v0.79.0 §4 — inbound-gate refusal (unread operator message pending). The
# refusal consumes no timeout budget; from the 3rd consecutive refusal per turn
# the sterner variant is returned + a WARN counter logged (Sol r2-5: no hard
# force-end primitive exists — this is soft anti-livelock).
_ASK_REFUSAL = (
    "the operator sent a message you have not seen — it will arrive next "
    "turn; end your turn now and re-ask after reading it"
)
_ASK_REFUSAL_STERN = (
    "STOP ASKING. The operator has a message waiting that you have NOT read. "
    "It is delivered to you the moment you end this turn. Do not ask another "
    "question — END YOUR TURN NOW, read the operator's message, then decide."
)
_ASK_REFUSAL_ESCALATE_AT = 3


def _validate_ask_args(body: dict) -> tuple[str, list, float] | None:
    """Validate + clamp the `ask` request body.

    Returns ``(question, options, clamped_timeout_s)`` on success, or
    ``None`` on any validation failure (caller maps to ``invalid_args``).

    v0.79.0 §4: ``options: []`` is now ACCEPTED (a free-text numbered anchor);
    a non-empty list still requires ``_ASK_MIN_OPTIONS..MAX``, unique, non-empty
    labels within the length cap. All validation lives here server-side (the
    channel subprocess transmits raw args and lets this gate refuse — r8-1).
    """
    question = body.get("question")
    if (not isinstance(question, str) or not question
            or len(question) > _ASK_MAX_QUESTION_LEN):
        return None
    options = body.get("options")
    if not isinstance(options, list):
        return None
    if len(options) != 0 and not (_ASK_MIN_OPTIONS <= len(options) <= _ASK_MAX_OPTIONS):
        return None
    if any(
        not isinstance(o, str) or not o or len(o) > _ASK_MAX_LABEL_LEN
        for o in options
    ):
        return None
    if len(set(options)) != len(options):
        return None
    try:
        timeout_s = float(body.get("timeout_s", _ASK_DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return None
    timeout_s = min(max(timeout_s, _ASK_MIN_TIMEOUT_S), _ASK_MAX_TIMEOUT_S)
    return question, options, timeout_s


def _canonical_question(question: str, number: int) -> str:
    """v0.79.0 §4 — the DISPLAYED question prefix is ALWAYS the allocated
    durable number. Strip any agent-authored leading ``Q<digits>:`` and
    re-prefix with ``Q<number>: `` so the message, ``open_questions`` and the
    summary can never disagree."""
    import re
    stripped = re.sub(r"^\s*[Qq]\d+\s*:\s*", "", question)
    return f"Q{number}: {stripped}"


def _ask_settle_text(question: str, outcome: dict, options: list) -> str:
    """v0.79.0 §4 — render the pinned settle copy below the canonical question.

    answered ⇒ ``\\n✅ <chosen label>``; expired (no_answer) ⇒
    ``\\n⌛ expired — answer by text below``; cancelled via a fresh operator
    message ⇒ ``\\n🚫 superseded by your message below``; any other cancel ⇒
    ``\\n🚫 cancelled``.
    """
    o = outcome.get("outcome")
    if o == "answered":
        idx = outcome.get("option_index")
        label = (
            options[idx]
            if isinstance(idx, int) and 0 <= idx < len(options)
            else "?"
        )
        return question + _SETTLE_ANSWERED.format(label=label)
    if o == "cancelled":
        reason = outcome.get("reason")
        if reason == "superseded_by_text":
            return question + _SETTLE_SUPERSEDED
        if reason == "internal_error":
            return question + _SETTLE_INTERNAL_ERROR
        return question + _SETTLE_CANCELLED
    # no_answer / timeout.
    return question + _SETTLE_EXPIRED


def _ask_keyboard_finish(
    telegram_channel: Any, topic_id: int | None, message_id: int,
    question: str, options: list,
    *, on_settle: "Callable[[], Awaitable[None]] | None" = None,
) -> Callable[[dict], "Awaitable[None]"]:
    """Broker finish-hook (r3-B3 shape, mirrors ``hooks._perm_keyboard_finish``)
    -- the engagement_ask namespace's ONLY keyboard-message writer. Fires
    exactly once on outcome (delivered by the broker even if the posting
    HTTP handler was cancelled/disconnected) and edits the posted question
    message to show the resolution AND CLEARS the keyboard (v0.79.0 §4, the
    real S1: ``clear_keyboard=True`` sends an explicit empty markup so the
    settled question can never be re-tapped). ``_on_inline_callback`` never
    edits the message itself -- it only ``cq.answer()``s.

    ``on_settle`` (§4): a callback run once on any terminal outcome to close the
    question's entry in the registry ``open_questions`` ledger.
    """

    async def _finish(outcome: dict) -> None:
        text = _ask_settle_text(question, outcome, options)
        try:
            await telegram_channel.edit_topic_message(
                topic_id, message_id, text, clear_keyboard=True,
            )
        except Exception:  # noqa: BLE001 — finish hooks must never raise
            logger.warning(
                "ask keyboard finish-hook edit failed "
                "(topic=%s message_id=%s)", topic_id, message_id, exc_info=True,
            )
        if on_settle is not None:
            try:
                await on_settle()
            except Exception:  # noqa: BLE001 — never raise from a finish hook
                logger.warning(
                    "ask keyboard finish-hook on_settle failed "
                    "(topic=%s message_id=%s)", topic_id, message_id,
                    exc_info=True,
                )

    return _finish


def _resolve_active_driver() -> Any:
    """Resolve the live ``claude_code`` driver (``agent.active_claude_code_driver``)
    for the inbound gate + discrete-intent registration. Returns ``None`` when
    no driver is attached (unit tests / degraded boot) so the ask handler falls
    back to its pre-v0.79.0 behavior (post + await, no gate/intent)."""
    try:
        import agent as _agent_mod
        return getattr(_agent_mod, "active_claude_code_driver", None)
    except Exception:  # noqa: BLE001
        return None


async def _maybe_allocate_number(engagement_registry: Any, eng_id: str) -> int | None:
    """Allocate the next durable Q-number (getattr-tolerant — a fake registry
    without the method leaves questions un-numbered)."""
    alloc = getattr(engagement_registry, "allocate_question_number", None)
    if alloc is None:
        return None
    try:
        return await alloc(eng_id)
    except Exception:  # noqa: BLE001 — numbering must never break the ask
        logger.warning("allocate_question_number failed (eng=%s)", eng_id[:8],
                       exc_info=True)
        return None


def _make_ask(
    telegram_channel: Any, engagement_registry: Any,
) -> Handler:
    """POST /internal/channel/ask — casa_engagement_channel's `ask` MCP tool.

    v0.75.0 (W5): registers on ``verdict_broker.BROKER`` (namespace
    ``"engagement_ask"``, scope = engagement_id), posts a tappable
    multiple-choice keyboard via the broker-owned shielded setup task
    (``ensure_posted``), then awaits the operator's tap.

    Body: ``{engagement_id, request_id, question, options: [str], timeout_s}``.
    Response: ``{"ok": True, "outcome": "answered", "option": <label>,
    "option_index": <int>}`` | ``{"ok": True, "outcome": "no_answer"}`` |
    ``{"ok": False, "error": <code>}``.

    A dropped HTTP connection (the caller's transport, not a logical
    cancel) leaves the broker request live for a same-``request_id`` retry
    to reattach -- ``await_result``'s shielded future is unaffected by this
    handler's own task being cancelled. Genuine caller cancellation is the
    separate explicit ``ask_cancel`` route (``_make_ask_cancel``).
    """

    async def handler(request: web.Request) -> web.Response:
        from verdict_broker import BROKER
        from channels.output_sequencer import ASK_TOOL

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_args"})
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "invalid_args"})

        eng_id = body.get("engagement_id")
        request_id = body.get("request_id")
        if not eng_id or not request_id:
            return web.json_response({"ok": False, "error": "invalid_args"})

        validated = _validate_ask_args(body)
        if validated is None:
            return web.json_response({"ok": False, "error": "invalid_args"})
        question, options, timeout_s = validated

        rec = engagement_registry.get(eng_id)
        if rec is None:
            return web.json_response({"ok": False, "error": "unknown_engagement"})
        if getattr(rec, "status", None) not in ("active", "idle"):
            return web.json_response({"ok": False, "error": "engagement_terminal"})

        driver = _resolve_active_driver()
        projection_hash = body.get("projection_hash")

        async def _advance_first_contact() -> None:
            advance = getattr(
                engagement_registry, "advance_interaction_state", None)
            if advance is not None:
                await advance(eng_id, "first_contact")

        # INBOUND GATE (§4): an unseen operator message means "end your turn" —
        # applies to EVERY kind of ask (button and free-text anchor). Consumes
        # no timeout budget; escalates from the 3rd consecutive refusal.
        def _refusal_response() -> web.Response:
            n = driver.record_ask_refusal(eng_id)
            copy = (
                _ASK_REFUSAL_STERN if n >= _ASK_REFUSAL_ESCALATE_AT
                else _ASK_REFUSAL
            )
            return web.json_response({
                "ok": False, "error": "unread_inbound",
                "message": copy, "refusal_count": n,
            })

        # --- FREE-TEXT ANCHOR (§4): options: [] posts a numbered anchor with
        # NO keyboard, registered in open_questions; the NEXT operator text
        # settles it (driver-side). Non-blocking — no broker request, no tap.
        # Posting is RELAY-DEFERRED (§2, review C1): the handler registers+arms
        # a discrete-send intent whose poster posts the numbered anchor, and the
        # relay posts it at the ask tool_use block (AFTER any preceding
        # narration). No driver/hash ⇒ eager fallback (pre-v0.79 behavior). ---
        if not options:
            if driver is not None and driver.inbound_unread_depth(eng_id) > 0:
                return _refusal_response()

            # F5: register the discrete-send intent and check for a REATTACH
            # BEFORE allocating a Q-number — parity with the button-ask reattach
            # (a transport retry must NOT burn a fresh number or post a second
            # anchor). ``created_intent`` is True only on the genuinely-first
            # attempt; None when there is no live sequencer (eager fallback).
            created_intent: bool | None = None
            if driver is not None and projection_hash:
                res = driver.register_send_intent(
                    engagement_id=eng_id, request_id=request_id,
                    tool_name=ASK_TOOL, projection_hash=projection_hash,
                    poster=_noop_poster,
                )
                if res is not None:
                    _intent, created_intent = res
                    if not created_intent:
                        # REATTACH: reuse the first attempt's outcome. If it
                        # already posted, return its id; if still UNRESOLVED,
                        # AWAIT the same bounded resolution and map None/timeout/
                        # failed to ok:false (F5 fail-closed) — never ok:true on
                        # an unresolved intent. No new number, no second anchor.
                        prior = driver.send_intent_outcome(eng_id, request_id)
                        if prior is None:
                            prior = await _await_deferred_post(
                                driver, eng_id, request_id)
                        if prior is None or not prior.get("ok"):
                            return web.json_response(
                                {"ok": False, "error": "delivery_failed"})
                        return web.json_response({
                            "ok": True, "outcome": "anchored",
                            "question_number": None,
                            "message_id": prior.get("message_id"),
                        })

            # First attempt (created intent) OR eager fallback: allocate the
            # durable number + build the poster.
            number = await _maybe_allocate_number(engagement_registry, eng_id)
            display = _canonical_question(question, number) if number else question

            async def _post_anchor() -> int | None:
                try:
                    mid = await telegram_channel.send_response_to_topic(
                        rec.topic_id, display)
                except Exception:  # noqa: BLE001
                    logger.warning("free-text anchor post failed (eng=%s)",
                                   eng_id[:8], exc_info=True)
                    return None
                if not isinstance(mid, int):
                    return None
                # open_questions registered ONLY after a successful post so a
                # crash before the relay reaches the block leaves NO dangling
                # ledger entry.
                if number is not None:
                    add = getattr(engagement_registry, "add_open_question", None)
                    if add is not None:
                        await add(eng_id, number, mid, text=display,
                                  kind="anchor")
                # W-R2: a posted anchor also hands the ball to the operator →
                # ⏳ waiting for your reply (driven from the ask lifecycle). The
                # next operator text settles it driver-side (_settle_open_anchor
                # recomputes the status back to working when none remain).
                if driver is not None:
                    note = getattr(driver, "note_ask_waiting", None)
                    if note is not None:
                        await note(eng_id)
                await _advance_first_contact()
                return mid

            if created_intent:
                # DEFERRED (relay-mediated) created path: install the poster,
                # ARM, and AWAIT the outcome fail-closed (F3/F5).
                driver.set_send_intent_poster(eng_id, request_id, _post_anchor)
                driver.arm_send_intent(eng_id, request_id)
                outcome = await _await_deferred_post(driver, eng_id, request_id)
                if outcome is None or not outcome.get("ok"):
                    return web.json_response(
                        {"ok": False, "error": "delivery_failed"})
                return web.json_response({
                    "ok": True, "outcome": "anchored",
                    "question_number": number,
                    "message_id": outcome.get("message_id"),
                })

            # EAGER fallback (no live sequencer): post the anchor now.
            mid = await _post_anchor()
            if mid is None:
                return web.json_response({"ok": False, "error": "delivery_failed"})
            return web.json_response({
                "ok": True, "outcome": "anchored",
                "question_number": number, "message_id": mid,
            })

        # --- BUTTON ask ---------------------------------------------------
        # Register the discrete-send INTENT (pending) at the ingress boundary for
        # idempotent transport-retry REATTACHMENT (§2(1)). The REAL relay-invoked
        # poster is installed just before we ARM (below) — posting is
        # RELAY-DEFERRED (§2, review C1): the relay posts the keyboard at the
        # ask's tool_use block, AFTER any preceding narration in the same frame.

        def _ask_static_meta() -> dict:
            # F1 (Sol r3): the keyboard's STATIC metadata (options + topic_id +
            # operator_id), seeded ATOMICALLY at broker creation. The old code
            # seeded meta AFTER register (``if created: req.meta.update(...)``)
            # ONLY on the main path, which lost the metadata whenever a
            # concurrent same-request_id RETRY created the broker request first:
            # the first attempt, suspended in number allocation, resumed to find
            # ``created=False`` and skipped the init, leaving meta =
            # {"message_id": ...} only ⇒ every tap rejected (topic_id/operator_id
            # both absent). Now BOTH the reattach path and the main path pass
            # ``meta=`` to ``register`` (a single synchronous op — register only
            # seeds meta on creation, with no await between), so whichever call
            # wins the create race installs the complete static metadata.
            return {
                "options": options,
                "topic_id": rec.topic_id,
                "operator_id": rec.origin.get("user_id"),
            }

        intent_registered = False
        if driver is not None and projection_hash:
            res = driver.register_send_intent(
                engagement_id=eng_id, request_id=request_id,
                tool_name=ASK_TOOL, projection_hash=projection_hash,
                poster=_noop_poster,
            )
            if res is not None:
                _intent, created_intent = res
                if not created_intent:
                    # F2 (was N1): a same-request_id retry REATTACHES (§2(1)) —
                    # whether the relay has already posted (prior has a
                    # message_id) OR the first attempt is still in flight and the
                    # keyboard has not posted yet (not-yet-posted, armed). EITHER
                    # WAY: NO new number allocation, NO second keyboard, NO eager
                    # fallback. Reattach to the broker request (idempotent by
                    # request_id) and await the same tap outcome. The old code
                    # only took this path when a message_id was recorded and
                    # otherwise fell through — allocating a fresh Q-number and
                    # posting a SECOND keyboard eagerly (the probe: Q2 posting
                    # before the relay's Q1, both ledger entries surviving).
                    # F1: create-with-metadata atomically. If THIS reattach wins
                    # the create race (the first attempt is still suspended in
                    # number allocation), it seeds the complete static metadata;
                    # if the request already exists, ``meta`` is ignored (register
                    # only seeds on creation) and the existing meta is reused.
                    req, _c = BROKER.register(
                        namespace="engagement_ask", scope=eng_id,
                        request_id=request_id, timeout_s=timeout_s,
                        meta=_ask_static_meta(),
                    )
                    outcome = await BROKER.await_result(req)
                    return _ask_outcome_response(outcome, options)
                intent_registered = True

        # INBOUND GATE (§4): an unseen operator message means "end your turn".
        if driver is not None and driver.inbound_unread_depth(eng_id) > 0:
            if intent_registered:
                driver.cancel_send_intent(eng_id, request_id)  # tombstone
            return _refusal_response()

        # Reserve the operator-message generation for the post-then-recheck race.
        gen_at_entry = (
            driver.inbound_generation(eng_id) if driver is not None else 0)

        number = await _maybe_allocate_number(engagement_registry, eng_id)
        display = _canonical_question(question, number) if number else question

        # F1: create-with-metadata atomically (STATIC meta seeded at creation so
        # a fast tap never sees incomplete metadata — r3-B3 fast-tap — AND a
        # concurrent reattach that created the request first still finds it
        # complete). message_id + finish_hook are set later by the broker-owned
        # setup task (r8-B3). ``meta`` is ignored if the request already exists.
        req, _created = BROKER.register(
            namespace="engagement_ask", scope=eng_id, request_id=request_id,
            timeout_s=timeout_s, meta=_ask_static_meta(),
        )

        # W-R2 linearization pin (Sol r2-1): the finish hook can become runnable
        # (a FAST TAP) before ``_post_ask`` finishes registering the open
        # question and setting ⏳ waiting. Gate the settlement recompute behind
        # this event — set by ``_post_ask`` ONLY after durable registration + the
        # waiting submission — so the recompute's revision is always allocated
        # LAST and a fast tap can never leave the summary stuck-waiting. The
        # event is ALWAYS set by ``_post_ask``'s finally (even on supersede /
        # add failure), and the finish hook exists only once ``_post_ask``
        # reached ``ensure_posted`` (which wires it), so this wait cannot hang.
        _ask_registered = asyncio.Event()

        async def _close_question() -> None:
            await _ask_registered.wait()
            if number is not None:
                close = getattr(engagement_registry, "close_open_question", None)
                if close is not None:
                    await close(eng_id, number)
            # Recompute the summary status from the remaining open questions
            # (still ⏳ waiting while any question is open; ⚙️ working once none
            # remain and the turn is running).
            if driver is not None:
                recompute = getattr(driver, "recompute_engagement_status", None)
                if recompute is not None:
                    await recompute(eng_id)

        # The DEFERRED poster (§2, review C1): the relay invokes this at the
        # ask's tool_use block (or the slot/intent-timeout watcher posts it
        # out-of-band). It posts the keyboard + wires the finish hook +
        # message_id via ``ensure_posted`` (post-once contract preserved), then
        # continues REACTIVELY off the posted message id: generation re-check,
        # open_questions registration, first-contact advance. Registering the
        # open question only AFTER a successful, non-superseded post means a
        # crash before the relay reaches the block leaves NO dangling ledger
        # entry — the broker TTL expires the ask instead.
        async def _post_ask() -> int | None:
            try:
                await BROKER.ensure_posted(
                    req,
                    lambda: telegram_channel.post_options_keyboard(
                        engagement_id=eng_id, request_id=request_id,
                        question=display, options=options),
                    lambda mid: _ask_keyboard_finish(
                        telegram_channel, rec.topic_id, mid, display, options,
                        on_settle=_close_question),
                )
                mid = req.meta.get("message_id")
                if not isinstance(mid, int):
                    # ensure_posted unregistered the request (post raised/None) →
                    # await_result below returns delivery_failed.
                    return None
                # GENERATION RE-CHECK (§4, Sol r1-4 — reserve→post→re-check, now
                # relay-mediated): an operator envelope that arrived between
                # reserve and post supersedes this ask — settle it (broker cancel
                # → finish hook renders the superseded copy + clears buttons),
                # consuming no timeout budget.
                superseded = (
                    driver is not None
                    and driver.inbound_generation(eng_id) != gen_at_entry
                )
                if superseded:
                    BROKER.cancel(
                        namespace="engagement_ask", scope=eng_id,
                        request_id=request_id, reason="superseded_by_text",
                    )
                else:
                    if number is not None:
                        add = getattr(
                            engagement_registry, "add_open_question", None)
                        if add is not None:
                            try:
                                await add(eng_id, number, mid, text=display)
                            except Exception:  # noqa: BLE001 — F6 strict-persist
                                # The ledger write failed AFTER the keyboard
                                # posted. Fail closed: settle the keyboard
                                # (internal-error copy via the finish hook) and
                                # refuse — a live-tappable keyboard the boot
                                # reconciler can never see is worse than a
                                # withdrawn question.
                                logger.warning(
                                    "engagement %s: add_open_question failed — "
                                    "withdrawing ask Q%s", eng_id[:8], number,
                                    exc_info=True,
                                )
                                BROKER.cancel(
                                    namespace="engagement_ask", scope=eng_id,
                                    request_id=request_id, reason="internal_error",
                                )
                                return None
                    # W-R2: a successful, non-superseded ask post → ⏳ waiting for
                    # your reply, driven from the ask LIFECYCLE (not the turn
                    # result). Ordered BEFORE any settlement recompute by the
                    # ``_ask_registered`` pin (set in the finally below).
                    if driver is not None:
                        note = getattr(driver, "note_ask_waiting", None)
                        if note is not None:
                            await note(eng_id)
                # W2/Sol B9 (Task 7): asking is an outbound agent action —
                # advance only after the keyboard actually posted.
                await _advance_first_contact()
                return mid
            finally:
                # Unblock the (possibly already-runnable) settlement path: the
                # registration + waiting submission above are now durable.
                _ask_registered.set()

        if intent_registered:
            # Install the real poster and ARM — the point of no return
            # (validation passed + broker registered). Only armed intents are
            # postable (§2(2)); the relay posts at the ask's tool_use block.
            driver.set_send_intent_poster(eng_id, request_id, _post_ask)
            driver.arm_send_intent(eng_id, request_id)
        else:
            # EAGER fallback (no live sequencer / degraded boot): post now.
            await _post_ask()

        # Shielded future (in await_result): a CancelledError here (transport
        # disconnect) propagates to OUR caller without cancelling the broker's
        # shared future -- the request stays live for a same-id reattach. The
        # future is decoupled from posting (resolved by the tap finish hook), so
        # nothing here needs the posted message id synchronously.
        outcome = await BROKER.await_result(req)
        return _ask_outcome_response(outcome, options)

    return handler


async def _await_deferred_post(driver: Any, eng_id: str, request_id: str) -> dict | None:
    """F3 fail-closed: await a deferred ask/reply/anchor intent's resolution so
    the handler returns ``ok`` ONLY when the post actually landed. Degrades to
    ``None`` (old immediate-return behavior) when the driver predates the await
    seam — an ``ok:true`` response with a failed post is then still impossible on
    the live path (the real driver always exposes it)."""
    awaiter = getattr(driver, "await_send_intent", None)
    if awaiter is None:
        return None
    try:
        return await awaiter(eng_id, request_id)
    except Exception:  # noqa: BLE001 — never wedge the handler on the await seam
        logger.debug("await_send_intent failed (eng=%s)", eng_id[:8], exc_info=True)
        return None


async def _noop_poster() -> int | None:
    """Placeholder poster used ONLY between an intent's early registration (for
    idempotent transport-retry reattach detection) and the point where the ask/
    reply handler installs the REAL relay-invoked poster via
    ``set_send_intent_poster`` — always before ARMING (§2(2), review C1). The
    relay never invokes this: a pending intent is never postable, and by arm
    time the real poster is in place."""
    return None


def _ask_outcome_response(outcome: dict, options: list) -> web.Response:
    """Map a broker ask outcome to the tool's JSON response."""
    o = outcome.get("outcome")
    if o == "answered":
        idx = outcome["option_index"]
        return web.json_response({
            "ok": True, "outcome": "answered",
            "option": options[idx], "option_index": idx,
        })
    if o == "no_answer":
        return web.json_response({"ok": True, "outcome": "no_answer"})
    if o == "cancelled":
        if outcome.get("reason") == "superseded_by_text":
            return web.json_response({
                "ok": False, "error": "superseded", "message": _ASK_REFUSAL,
            })
        if outcome.get("reason") == "internal_error":
            return web.json_response({
                "ok": False, "error": "internal_error",
                "message": ("the question could not be recorded — it was "
                            "withdrawn; end your turn and re-ask"),
            })
        return web.json_response({"ok": False, "error": "cancelled"})
    # delivery_failed (keyboard post raised or returned None, r10-B3).
    return web.json_response({"ok": False, "error": "delivery_failed"})


def _make_ask_cancel() -> Handler:
    """POST /internal/channel/ask_cancel — explicit caller cancellation.

    v0.75.0 (W5): the `ask` MCP tool's ``finally`` calls this on genuine
    cancellation (NOT a transport retry) so a same-id reattach can never
    resurrect a stale keyboard tap. Always ``{"ok": True}`` -- cancelling an
    already-resolved or never-registered request is a harmless no-op
    (``BROKER.cancel`` returns False but we don't surface that distinction;
    the caller only wants "stop waiting for this", which is unconditionally
    true after the call returns).
    """

    async def handler(request: web.Request) -> web.Response:
        from verdict_broker import BROKER

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_args"})
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "invalid_args"})

        eng_id = body.get("engagement_id")
        request_id = body.get("request_id")
        if not eng_id or not request_id:
            return web.json_response({"ok": False, "error": "invalid_args"})

        BROKER.cancel(
            namespace="engagement_ask", scope=eng_id, request_id=request_id,
            reason="caller_cancelled",
        )
        return web.json_response({"ok": True})

    return handler


def _make_update_state(telegram_channel: Any) -> Handler:
    """POST /internal/channel/update_state — channel server → casa-main.

    Phase 2 (Task 23): the per-engagement channel server flips the topic
    title's state emoji (awaiting / active) via this handler when permission
    is requested / verdict received. Terminal-state transitions (completed /
    failed / cancelled) come from ``_finalize_engagement`` directly via the
    same ``update_topic_state`` helper on the channel — no internal POST
    needed in that path.

    Body shape: ``{engagement_id, new_state}``. Channel decides which states
    are meaningful (this handler just forwards). Failure returns
    ``update_failed`` so the caller can decide whether to retry.
    """

    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad_json"})
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "bad_json"})

        eng_id = body.get("engagement_id")
        new_state = body.get("new_state")
        if not eng_id or not new_state:
            return web.json_response({"ok": False, "error": "bad_params"})

        try:
            await telegram_channel.update_topic_state(
                engagement_id=eng_id, new_state=new_state,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "update_state failed (engagement=%s state=%s): %s",
                eng_id, new_state, exc,
            )
            return web.json_response({"ok": False, "error": "update_failed"})

        return web.json_response({"ok": True})

    return handler


def _make_channel_handlers(
    *, telegram_channel: Any, engagement_registry: Any,
    record_reply: Callable[[str, str], None] | None = None,
) -> dict[str, Handler]:
    """Return a path → handler dict for /internal/channel/* POSTs.

    Phase 1: ``send_to_topic``.
    Phase 2: ``post_inline_keyboard`` (Task 19), ``permission_verdict`` (Task 21),
    ``update_state`` (Task 23).
    v0.75.0 (W5): ``ask`` / ``ask_cancel`` (Task 3).
    v0.75.0 (W1): ``record_reply`` hook threads reply() texts to the
    claude_code driver's live topic-stream relay de-dup.
    Phase 2+ will extend with ``set_progress``, ``typing``, etc. — see spec §A.3.
    """
    return {
        "/internal/channel/send_to_topic": _make_send_to_topic(
            telegram_channel=telegram_channel,
            engagement_registry=engagement_registry,
            record_reply=record_reply,
        ),
        "/internal/channel/post_inline_keyboard": _make_post_inline_keyboard(
            telegram_channel=telegram_channel,
            engagement_registry=engagement_registry,
        ),
        "/internal/channel/permission_verdict": _make_permission_verdict(
            engagement_registry=engagement_registry,
        ),
        "/internal/channel/update_state": _make_update_state(
            telegram_channel=telegram_channel,
        ),
        "/internal/channel/ask": _make_ask(
            telegram_channel=telegram_channel,
            engagement_registry=engagement_registry,
        ),
        "/internal/channel/ask_cancel": _make_ask_cancel(),
    }


def _make_channel_get_handlers(
    *, engagement_registry: Any,
) -> dict[str, Handler]:
    """Return a path → handler dict for /internal/channel/* GETs.

    v0.75.0 (W5/Sol B3,B4): the ``permission_pending`` long-poll (Task 21)
    was removed — verdicts now flow through ``verdict_broker.BROKER``
    directly (see ``_make_permission_verdict``), no queue/poll needed. Kept
    as an (empty, for now) factory so ``casa_core``'s generic
    ``router.add_get`` loop over this dict needs no changes when a real GET
    handler is added here in the future.
    """
    # engagement_registry isn't strictly needed by the GET handler family
    # today, but keeping the symmetric (engagement_registry=) signature lets
    # a future GET (e.g. /internal/channel/status?engagement_id=) reuse it
    # without adding another factory.
    del engagement_registry
    return {}
