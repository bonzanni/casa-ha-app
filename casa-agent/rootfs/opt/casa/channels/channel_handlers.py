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
import inspect
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiohttp import web

from settle_gate import confirmed_settle_edit

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

        # §A3(a) LIVE-PENDING REPLY GATE (Sol r1-9 + r5-2): refuse this reply
        # while the engagement has a LIVE unresolved question. The check runs
        # UNDER THE SAME per-engagement ask-maintenance lock as the ask ingress
        # reservation (linearizing the reply against the reservation — closing the
        # r5-2 race where parallel ask+reply both pass before the ask reaches
        # durable ownership); the lock is held for the CHECK ONLY. Gating on
        # actual pending state means reply-then-ask, a tap-answered ask (broker
        # empty), an EXPIRED ask (no live request), and an answered-but-
        # unconfirmed-settle anchor (the answered/reserved split) are all allowed.
        _reply_gate_lock = _ask_maint_lock(driver, engagement_id)
        if _reply_gate_lock is not None:
            async with _reply_gate_lock:
                if _ask_pending_predicate(driver, engagement_id):
                    return _reply_pending_response(driver, engagement_id)

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
# A4 · F-BTN (Sol r3-7b): an agent-supplied ``short`` ≤ 25 so ``"n · <short>"``
# (prefix ≤ 5 chars) keeps the rendered button within the 30-char button cap.
_ASK_MAX_SHORT_LEN = 25
_ASK_MAX_QUESTION_LEN = 1024
_ASK_MIN_TIMEOUT_S = 30.0
_ASK_MAX_TIMEOUT_S = 570.0
_ASK_DEFAULT_TIMEOUT_S = 300.0

# v0.79.0 §4 — pinned settle copy (appended below the canonical question text
# when the keyboard settles; the keyboard is cleared via clear_keyboard=True).
_SETTLE_ANSWERED = "\n✅ {label}"
# F-EXPIRE (v0.83.0, A2a): a live-ask keyboard that expires unanswered now
# SUSPENDS the engagement (operator-away) rather than inviting an immediate
# re-ask, so the settle copy tells the operator the engagement is paused and how
# to resume it.
_SETTLE_EXPIRED = "\n⌛ expired — engagement paused; reply here to continue"
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

# F-EXPIRE (v0.83.0, A2a) — operator-away copies.
# The enriched ``no_answer`` response returned when a live ask expires: the
# engagement is now PAUSED and the agent must end its turn silently rather than
# re-ask (the live incident was a 21-ask loop).
_ASK_PAUSED_MESSAGE = (
    "The operator did not answer in time. The engagement is now PAUSED — end "
    "your turn silently (no sign-off message). Do NOT re-ask: your question "
    "stays on record and the operator's reply will start your next turn."
)
# The refusal returned to EVERY further ask while operator-away, with no broker
# registration / keyboard / timeout burn.
_ASK_AWAY_REFUSAL = (
    "The operator is away — your last question expired unanswered. END YOUR "
    "TURN NOW, silently. Do not ask again; the operator's return starts your "
    "next turn."
)

# v0.83.0 §A3 — F-ORDER structural gates. ``question_pending`` is returned by the
# reply gate (a) and the ask-ingress stacking gate (c) when a question is already
# LIVE for the engagement. ``{n}`` is filled from the pending question number
# when known; the number-less variant is used when it cannot be resolved.
_REPLY_PENDING_NUMBERED = (
    "you have an open question (Q{n}) — end your turn and wait for the answer"
)
_REPLY_PENDING_GENERIC = (
    "you have an open question — end your turn and wait for the answer"
)
_ASK_PENDING_NUMBERED = (
    "Q{n} is still open — wait for the answer (end your turn) instead of asking "
    "another question"
)
_ASK_PENDING_GENERIC = (
    "a question is still open — wait for the answer (end your turn) instead of "
    "asking another question"
)
# §A3(c): the withdrawn-anchor copy edited over an orphan whose ledger write
# failed after posting (RAW-wire edit, never edit_discrete — see _post_anchor).
_ANCHOR_WITHDRAWN = "⚠️ internal error — question withdrawn, please resend"
# §A3(c): the compensated / withdrawn ask's tool response copy (add-failure).
_ASK_INTERNAL_ERROR_MSG = (
    "the question could not be recorded — it was withdrawn; end your turn and "
    "re-ask"
)


def _ask_maint_lock(driver: Any, eng_id: str) -> Any:
    """The per-engagement ask-maintenance lock, or ``None`` when the driver
    predates the §A3 gate seam (unit fakes / degraded boot) — the reply /
    stacking gates then simply don't engage. Getattr-tolerant."""
    fn = getattr(driver, "ask_maintenance_lock", None) if driver is not None else None
    return fn(eng_id) if fn is not None else None


def _clear_ask_marker(driver: Any, eng_id: str, request_id: str) -> None:
    """Clear the ingress marker (CAS on request_id), getattr-tolerant."""
    fn = getattr(driver, "clear_ask_inflight", None) if driver is not None else None
    if fn is not None:
        fn(eng_id, request_id)


def _ask_pending_predicate(
    driver: Any, eng_id: str, *, exclude_request_id: str | None = None,
) -> bool:
    """§A3 live-pending predicate (shared by the reply gate + the ask stacking
    gate). True iff the engagement has a LIVE unresolved question: a live broker
    ask (``BROKER.pending`` non-empty) OR an unanswered free-text anchor (the
    driver's EFFECTIVE view — Task 6 ``answered`` + Task 7 reserved excluded) OR
    the ``ask_inflight`` ingress marker set to a DIFFERENT request_id (the
    marker→durable-ownership gap). Evaluated UNDER the ask-maintenance lock."""
    from verdict_broker import BROKER
    if BROKER.pending(namespace="engagement_ask", scope=eng_id):
        return True
    if driver is None:
        return False
    anchor_fn = getattr(driver, "effective_open_anchor", None)
    if anchor_fn is not None:
        try:
            if anchor_fn(eng_id) is not None:
                return True
        except Exception:  # noqa: BLE001 — degrade to "no anchor"
            logger.debug("effective_open_anchor read failed", exc_info=True)
    marker_fn = getattr(driver, "ask_inflight", None)
    marker = marker_fn(eng_id) if marker_fn is not None else None
    return marker is not None and marker != exclude_request_id


def _pending_question_number(driver: Any, eng_id: str) -> int | None:
    """The smallest EFFECTIVE-open question number for the refusal copy, or
    ``None`` when unknown (e.g. a button ask reserved but not yet ledger-added,
    or a degraded registry). Number-less copy is acceptable then."""
    if driver is None:
        return None
    fn = getattr(driver, "_effective_open_question_numbers", None)
    if fn is None:
        return None
    try:
        nums = fn(eng_id)
    except Exception:  # noqa: BLE001
        return None
    return min(nums) if nums else None


def _reply_pending_response(driver: Any, eng_id: str) -> web.Response:
    n = _pending_question_number(driver, eng_id)
    msg = (
        _REPLY_PENDING_NUMBERED.format(n=n) if n is not None
        else _REPLY_PENDING_GENERIC
    )
    return web.json_response(
        {"ok": False, "error": "question_pending", "message": msg})


def _ask_pending_response(driver: Any, eng_id: str) -> web.Response:
    n = _pending_question_number(driver, eng_id)
    msg = (
        _ASK_PENDING_NUMBERED.format(n=n) if n is not None
        else _ASK_PENDING_GENERIC
    )
    return web.json_response(
        {"ok": False, "error": "question_pending", "message": msg})


def _record_intent_internal_error(
    driver: Any, eng_id: str, request_id: str,
) -> None:
    """§A3(c) allocation-failure: tombstone the intent with an internal_error
    OUTCOME so a same-request_id retry reattaches and short-circuits (no fresh
    post). Degrades to a bare cancel on a driver without the seam."""
    fn = getattr(driver, "record_send_intent_refusal", None)
    if fn is not None:
        try:
            fn(eng_id, request_id, {"ok": False, "error": "internal_error"})
            return
        except Exception:  # noqa: BLE001 — fall back to a bare tombstone
            logger.debug("record_send_intent_refusal(internal) failed", exc_info=True)
    cancel = getattr(driver, "cancel_send_intent", None)
    if cancel is not None:
        cancel(eng_id, request_id)


def _operator_away_active(driver: Any, eng_id: str) -> bool:
    """§A2.4 gate read — getattr-tolerant so a driver without operator-away
    support (unit fakes / degraded boot) simply never gates."""
    if driver is None:
        return False
    fn = getattr(driver, "operator_away_active", None)
    if fn is None:
        return False
    try:
        return bool(fn(eng_id))
    except Exception:  # noqa: BLE001 — a gate read must never wedge the ask
        logger.debug("operator_away_active read failed", exc_info=True)
        return False


def _away_refusal_payload() -> dict:
    """The canonical operator-away refusal body. Shared by the live refusal
    response AND the intent-refusal outcome recorded for a transport retry
    (Finding 1) so a reattaching retry returns byte-identical JSON."""
    return {"ok": False, "error": "operator_away", "message": _ASK_AWAY_REFUSAL}


def _away_refusal_response(driver: Any, eng_id: str) -> web.Response:
    """§A2.4 refusal — bump the per-episode away-refusal counter (Task 5's
    force-turn-boundary backstop reads it) and return the fixed refusal copy."""
    bump = getattr(driver, "record_away_refusal", None) if driver is not None else None
    if bump is not None:
        try:
            bump(eng_id)
        except Exception:  # noqa: BLE001 — the refusal copy stands either way
            logger.debug("record_away_refusal failed", exc_info=True)
    return web.json_response(_away_refusal_payload())


def _record_intent_refusal(driver: Any, eng_id: str, request_id: str) -> None:
    """§A2.4 (Finding 1): record the operator-away refusal OUTCOME on the intent
    instead of a bare cancel. A same-``request_id`` transport retry then hits the
    reattach path FIRST, reads this recorded outcome, and short-circuits to the
    SAME refusal — never awaiting the dead intent (→ ``delivery_failed``, anchor)
    nor re-registering a fresh broker request (→ timeout burn, no keyboard,
    button). Degrades to the bare cancel on a driver predating the seam."""
    fn = getattr(driver, "record_send_intent_refusal", None)
    if fn is not None:
        try:
            fn(eng_id, request_id, _away_refusal_payload())
            return
        except Exception:  # noqa: BLE001 — fall back to the bare tombstone
            logger.debug("record_send_intent_refusal failed", exc_info=True)
    driver.cancel_send_intent(eng_id, request_id)  # tombstone (pre-A2 behaviour)


def _unread_refusal_payload(copy: str) -> dict:
    """The refusal-count-FREE ``unread_inbound`` body recorded on the intent for a
    transport retry (Sol A2 wave-3, Finding 3). The LIVE refusal response carries
    ``refusal_count`` (a fresh bump); the recorded reattach copy omits it so a
    retry never re-bumps the counter."""
    return {"ok": False, "error": "unread_inbound", "message": copy}


def _record_intent_unread_refusal(
    driver: Any, eng_id: str, request_id: str, copy: str,
) -> None:
    """Sol A2 wave-3, Finding 3 (symmetric with :func:`_record_intent_refusal`):
    record the ``unread_inbound`` refusal OUTCOME on the intent instead of a bare
    cancel. A same-``request_id`` transport retry then hits the reattach path
    FIRST, reads this recorded outcome, and short-circuits to the SAME refusal —
    never awaiting the dead intent (→ the deferred-post budget → ``delivery_failed``,
    anchor) nor re-registering. Degrades to the bare cancel on a driver predating
    the seam."""
    fn = getattr(driver, "record_send_intent_refusal", None)
    if fn is not None:
        try:
            fn(eng_id, request_id, _unread_refusal_payload(copy))
            return
        except Exception:  # noqa: BLE001 — fall back to the bare tombstone
            logger.debug("record_send_intent_refusal(unread) failed", exc_info=True)
    cancel = getattr(driver, "cancel_send_intent", None)
    if cancel is not None:
        cancel(eng_id, request_id)


def _refused_intent_outcome(prior: Any) -> bool:
    """True iff a reattached intent's recorded outcome is a refusal the retry
    returns verbatim — an operator-away refusal (Finding 1) OR an unread-inbound
    refusal (Sol A2 wave-3, Finding 3). Both are terminal recorded outcomes; the
    retry returns them as-is rather than awaiting the dead intent."""
    return (
        isinstance(prior, dict)
        and prior.get("error") in ("operator_away", "unread_inbound")
    )


async def _no_answer_response(
    driver: Any, eng_id: str, request_id: str, req: Any,
) -> web.Response:
    """§A2.1 expiry — ENTER operator-away (generation-CAS via THIS waiter's own
    ``req.meta`` ``inbound_gen`` — Finding 2: never re-query the broker by key
    after the await, where a retired/reused tombstone could hand back a NEWER
    generation and re-wedge) and return the enriched PAUSED response. Degrades to
    the plain ``no_answer`` response on a driver without operator-away support
    (unit / eager fallback) so existing no-driver callers are byte-unchanged."""
    note = (
        getattr(driver, "note_operator_away", None)
        if driver is not None else None
    )
    if note is None:
        return web.json_response({"ok": True, "outcome": "no_answer"})
    # Finding 2: use the waiter's OWN req.meta (the handler holds ``req`` in both
    # the main and reattach paths; register returns the tombstone-backed req for
    # a retired key), never ``BROKER.get_meta`` by key after the await.
    meta = getattr(req, "meta", None)
    gen = meta.get("inbound_gen") if isinstance(meta, dict) else None
    if gen is not None:  # tolerate a missing gen — just skip the away entry
        try:
            res = note(eng_id, gen=gen)
            if inspect.isawaitable(res):
                await res
        except Exception:  # noqa: BLE001 — away entry is best-effort
            logger.debug("note_operator_away failed", exc_info=True)
    return web.json_response({
        "ok": True, "outcome": "no_answer", "engagement_paused": True,
        "message": _ASK_PAUSED_MESSAGE,
    })


async def _ask_final_response(
    outcome: dict, options: list, driver: Any, eng_id: str, request_id: str,
    req: Any,
) -> web.Response:
    """Map a broker ask outcome to the tool response, intercepting ``no_answer``
    to enter operator-away + return the PAUSED response (F-EXPIRE). Every other
    outcome delegates to ``_ask_outcome_response`` byte-identically."""
    if outcome.get("outcome") == "no_answer":
        return await _no_answer_response(driver, eng_id, request_id, req)
    return _ask_outcome_response(outcome, options)


def _validate_ask_args(
    body: dict,
) -> tuple[str, list, float, list] | None:
    """Validate + clamp the `ask` request body.

    Returns ``(question, options, clamped_timeout_s, shorts)`` on success, or
    ``None`` on any validation failure (caller maps to ``invalid_args``).
    ``options`` is the list of FULL labels (downstream sees these everywhere —
    body VERBATIM, broker meta, settle ✅, ``_ask_outcome_response``); ``shorts``
    is a PARALLEL list, one entry per option: the agent-supplied short label
    (str) or ``None``. The ONLY consumer of ``shorts`` is the keyboard.

    v0.79.0 §4: ``options: []`` is ACCEPTED (a free-text numbered anchor); a
    non-empty list still requires ``_ASK_MIN_OPTIONS..MAX``, unique, non-empty
    labels within the length cap.

    v0.83.0 (A4 · F-BTN): each option may be a plain ``str`` (unchanged) OR a
    ``{"label": str, "short": str}`` dict — ``label`` under the 48 cap and
    ``label.strip()`` non-empty; ``short`` ≤ 25 and ``short.strip()`` non-empty.
    Mixed str+dict lists are allowed. Duplicate FULL labels OR duplicate
    provided shorts are refused. The projection hash is computed client-side
    over the RAW args, so this server-side normalization does not affect relay
    matching. All validation lives here server-side (the channel subprocess
    transmits raw args and lets this gate refuse — r8-1).
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
    labels: list[str] = []
    shorts: list[str | None] = []
    for o in options:
        if isinstance(o, str):
            # str path UNCHANGED (enumerator/strip normalization is A6/Task 12).
            if not o or len(o) > _ASK_MAX_LABEL_LEN:
                return None
            labels.append(o)
            shorts.append(None)
        elif isinstance(o, dict):
            label = o.get("label")
            short = o.get("short")
            if (not isinstance(label, str) or not label.strip()
                    or len(label) > _ASK_MAX_LABEL_LEN):
                return None
            if (not isinstance(short, str) or not short.strip()
                    or len(short) > _ASK_MAX_SHORT_LEN):
                return None
            labels.append(label)
            shorts.append(short)
        else:
            return None
    if len(set(labels)) != len(labels):
        return None
    provided_shorts = [s for s in shorts if s is not None]
    if len(set(provided_shorts)) != len(provided_shorts):
        return None
    try:
        timeout_s = float(body.get("timeout_s", _ASK_DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return None
    timeout_s = min(max(timeout_s, _ASK_MIN_TIMEOUT_S), _ASK_MAX_TIMEOUT_S)
    return question, labels, timeout_s, shorts


def _canonical_question(question: str, number: int) -> str:
    """v0.79.0 §4 — the DISPLAYED question prefix is ALWAYS the allocated
    durable number. Strip any agent-authored leading ``Q<digits>:`` and
    re-prefix with ``Q<number>: `` so the message, ``open_questions`` and the
    summary can never disagree."""
    import re
    stripped = re.sub(r"^\s*[Qq]\d+\s*:\s*", "", question)
    return f"Q{number}: {stripped}"


def render_ask_body(number: "int | None", question: str, options: list) -> str:
    """v0.81.0 (W-R3, Sol r1-5) — the SINGLE canonical rendered ask body.

    Used IDENTICALLY by all four ask consumers so they can never disagree:
    the initial keyboard post, the finish-hook settlement base, the persisted
    ``open_questions[].text``, and boot reconciliation. If the displayed
    message and the persisted text ever diverged, a tap/reconcile would drop
    the option list — this one source prevents that bug.

    Format::

        Q<n>: <question>

        1. <opt0>
        2. <opt1>
        …

    EVERY option is rendered VERBATIM (no truncation, no ellipsis), 1-based
    numbered. A free-text anchor (``options == []``) renders the numbered
    question ALONE — no option list. ``number`` may be ``None`` (no durable
    number allocated / degraded boot), in which case the bare question is used
    without the ``Q<n>:`` prefix.

    No overflow path (Sol r1-5): the ``_validate_ask_args`` caps (question
    ≤1024, ≤8 options ≤48 each) keep this body well under Telegram's 4096-char
    limit (worst case ≈1.5 KB). A cap change that could exceed 4096 must REJECT
    in ``_validate_ask_args`` — never silently truncate here.
    """
    base = _canonical_question(question, number) if number else question
    if not options:
        return base
    numbered = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
    return f"{base}\n\n{numbered}"


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
    sleep: "Callable[[float], Awaitable[None]]" = asyncio.sleep,
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

    W-R1 (v0.81.0, Sol r2-2) — CONFIRMED-EDIT GATING: the settle edit can fail
    transiently (``edit_topic_message`` returns ``False`` on a timeout /
    non-'not-modified' BadRequest). Because the broker fires this hook exactly
    ONCE, a later tap cannot re-drive settlement. So bounds-retry the edit
    (``confirmed_settle_edit``: 3 attempts, 0.5→1→2 backoff, injected ``sleep``)
    and run ``on_settle`` (which closes the ledger entry) ONLY on a CONFIRMED
    edit. An unconfirmed edit leaves the keyboard live AND the ledger entry
    INTACT so the NEXT boot reconciliation (itself confirmed-edit gated) settles
    it — there is deliberately no later-tap re-drive.
    """

    async def _finish(outcome: dict) -> None:
        text = _ask_settle_text(question, outcome, options)
        confirmed = await confirmed_settle_edit(
            lambda: telegram_channel.edit_topic_message(
                topic_id, message_id, text, clear_keyboard=True),
            sleep=sleep,
        )
        if not confirmed:
            logger.warning(
                "ask keyboard finish-hook settle edit UNCONFIRMED after retries "
                "(topic=%s message_id=%s) — leaving keyboard live and the "
                "open-question ledger entry INTACT for boot reconciliation",
                topic_id, message_id,
            )
            return
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
    """Allocate the next durable Q-number, distinguishing ABSENT from RAISING
    (Sol r8-4). An ABSENT allocator (fake registry / degraded boot without the
    method) returns ``None`` → the legacy un-numbered degraded path. An allocator
    that RAISES PROPAGATES the exception so the caller can refuse the ask BEFORE
    any wire post (a successful, operator-visible, UNTRACKED ask would defeat the
    gap-free ``ask_inflight`` → durable-ownership handoff)."""
    alloc = getattr(engagement_registry, "allocate_question_number", None)
    if alloc is None:
        return None
    return await alloc(eng_id)


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
        # ``options`` = FULL labels (body/meta/settle/response see these);
        # ``shorts`` = parallel agent-supplied shorts (keyboard-only, A4 rule 3).
        question, options, timeout_s, shorts = validated

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
        def _refusal_response(record_intent: bool = False) -> web.Response:
            n = driver.record_ask_refusal(eng_id)
            copy = (
                _ASK_REFUSAL_STERN if n >= _ASK_REFUSAL_ESCALATE_AT
                else _ASK_REFUSAL
            )
            if record_intent:
                # Sol A2 wave-3, Finding 3: record the refusal-count-FREE outcome
                # on the intent (not a bare cancel) so a same-request_id retry
                # reattaches to it and returns unread_inbound IMMEDIATELY, never
                # awaiting the dead intent (→ deferred-post budget → delivery_failed).
                _record_intent_unread_refusal(driver, eng_id, request_id, copy)
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
            # F5: register the discrete-send intent and check for a REATTACH
            # BEFORE allocating a Q-number — parity with the button-ask reattach
            # (a transport retry must NOT burn a fresh number or post a second
            # anchor). ``created_intent`` is True only on the genuinely-first
            # attempt; None when there is no live sequencer (eager fallback).
            #
            # GATE ORDERING (Sol A2 wave-2, Finding 4): the reattach-outcome
            # check runs FIRST — BEFORE the unread and away gates — exactly like
            # the button path. Previously the unread-inbound gate ran first, so a
            # same-request_id retry whose original was refused ``operator_away``
            # could get ``unread_inbound`` (an inbound cleared away but is still
            # unread) instead of its RECORDED outcome. Order now mirrors the
            # button path: reattach → away gate → unread gate.
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
                        # Finding 1: a prior operator-away refusal recorded an
                        # ``operator_away`` outcome — return it verbatim instead
                        # of awaiting the dead intent (→ delivery_failed).
                        if _refused_intent_outcome(prior):
                            return web.json_response(prior)
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

            # F-EXPIRE (§A2.4) GATE: while operator-away, refuse a genuinely-new
            # anchor immediately — no number, no post, no broker. Placed AFTER the
            # reattach check so a transport retry of an already-posted anchor
            # still returns its recorded outcome above.
            if _operator_away_active(driver, eng_id):
                if created_intent:
                    # Finding 1: record the refusal OUTCOME (not a bare cancel)
                    # so a same-id retry reattaches to it above.
                    _record_intent_refusal(driver, eng_id, request_id)
                return _away_refusal_response(driver, eng_id)

            # INBOUND GATE (§4): an unseen operator message means "end your turn".
            # Placed AFTER the reattach + away checks (Finding 4) — a genuinely-new
            # anchor is refused here; a same-id retry never reaches this point. A
            # freshly-created intent records the unread_inbound OUTCOME (Sol A2
            # wave-3, Finding 3 — NOT a bare cancel) so a retry reattaches to it.
            if driver is not None and driver.inbound_unread_depth(eng_id) > 0:
                return _refusal_response(record_intent=bool(created_intent))

            # §A3(c) INGRESS RESERVATION (Sol r2-8/r3-6): under the ask-
            # maintenance lock, atomically CHECK the live-pending predicate and
            # CLAIM the ``ask_inflight`` marker. A second concurrent ask (any
            # kind) then sees the marker/predicate and refuses ``question_pending``
            # — making "one question at a time" structural. The lock is held for
            # the CHECK + marker ONLY, never across the post/await below. Sample
            # the operator-generation at this reserve point for the post-add
            # re-check (a message that lands between reserve and add is the answer).
            gen_at_entry = (
                driver.inbound_generation(eng_id) if driver is not None else 0)
            _anchor_lock = _ask_maint_lock(driver, eng_id)
            if _anchor_lock is not None:
                async with _anchor_lock:
                    if _ask_pending_predicate(
                            driver, eng_id, exclude_request_id=request_id):
                        if created_intent:
                            driver.cancel_send_intent(eng_id, request_id)
                        return _ask_pending_response(driver, eng_id)
                    driver.set_ask_inflight(eng_id, request_id)

            # First attempt (created intent) OR eager fallback: allocate the
            # durable number. A RAISING allocator (Sol r8-4) is TERMINAL BEFORE
            # any wire post — clear the marker, tombstone the intent with an
            # internal_error outcome (retries short-circuit), refuse. An ABSENT
            # allocator returns None → the un-numbered legacy degraded path.
            try:
                number = await _maybe_allocate_number(engagement_registry, eng_id)
            except Exception:  # noqa: BLE001
                logger.warning("anchor number allocation failed (eng=%s)",
                               eng_id[:8], exc_info=True)
                _clear_ask_marker(driver, eng_id, request_id)
                if created_intent:
                    _record_intent_internal_error(driver, eng_id, request_id)
                return web.json_response({"ok": False, "error": "internal_error"})
            # W-R3: canonical body (anchor ⇒ options == [] ⇒ numbered question
            # ALONE, no option list — unchanged from the pre-W-R3 anchor copy).
            display = render_ask_body(number, question, options)

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
                # DURABLE OWNERSHIP: register open_questions ONLY after a
                # successful post (a crash before the relay reaches the block
                # leaves NO dangling ledger entry). §A3(c) COMPENSATION (Sol
                # r5-5/r6-1): an ``add_open_question`` failure AFTER the wire post
                # leaves an orphan message — best-effort WITHDRAW-edit it via the
                # RAW wire primitive (never edit_discrete — this poster runs under
                # the sequencer lock on the relay task, no reacquisition) and
                # account the COMPOUND outcome (``mark_send_intent_compensated``:
                # high-water advances, intent resolves ok:false+compensated). The
                # exception NEVER escapes the poster.
                added = False
                if number is not None:
                    add = getattr(engagement_registry, "add_open_question", None)
                    if add is not None:
                        try:
                            await add(eng_id, number, mid, text=display,
                                      kind="anchor")
                            added = True
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "anchor add_open_question failed — withdrawing "
                                "(eng=%s Q%s)", eng_id[:8], number, exc_info=True)
                            await _withdraw_anchor(
                                telegram_channel, rec.topic_id, mid)
                            if driver is not None:
                                comp = getattr(
                                    driver, "mark_send_intent_compensated", None)
                                if comp is not None:
                                    try:
                                        await comp(eng_id, request_id, mid)
                                    except Exception:  # noqa: BLE001
                                        logger.debug(
                                            "compensate seam failed", exc_info=True)
                                _clear_ask_marker(driver, eng_id, request_id)
                            return None
                # Marker cleared SYNCHRONOUSLY at durable ownership (no
                # maintenance lock — the unanswered-anchor clause takes over
                # gap-free). In the ABSENT-allocator degraded mode (no number, no
                # add) this is the poster's terminal path and the one-question
                # invariant is UNAVAILABLE (Sol r9-4).
                _clear_ask_marker(driver, eng_id, request_id)
                # POST-ADD GENERATION RE-CHECK: an operator envelope that arrived
                # between reserve and add IS this anchor's answer — mark it
                # answered + settle instead of leaving it ⏳ waiting.
                gen_bumped = (
                    added and driver is not None
                    and driver.inbound_generation(eng_id) != gen_at_entry
                )
                if gen_bumped:
                    settle = getattr(driver, "settle_answered_anchor", None)
                    if settle is not None:
                        try:
                            await settle(eng_id, number)
                        except Exception:  # noqa: BLE001 — settle is best-effort
                            logger.debug("gen-recheck settle failed", exc_info=True)
                else:
                    # W-R2: a posted, un-answered anchor hands the ball to the
                    # operator → ⏳ waiting for your reply (driven from the ask
                    # lifecycle; the next operator text settles it driver-side).
                    if driver is not None:
                        note = getattr(driver, "note_ask_waiting", None)
                        if note is not None:
                            await note(eng_id)
                await _advance_first_contact()
                return mid

            try:
                if created_intent:
                    # DEFERRED (relay-mediated) created path: install the poster,
                    # ARM, and AWAIT the outcome fail-closed (F3/F5).
                    driver.set_send_intent_poster(eng_id, request_id, _post_anchor)
                    driver.arm_send_intent(eng_id, request_id)
                    outcome = await _await_deferred_post(driver, eng_id, request_id)
                    # §A3(c): the compensated add-failure maps to ok:false
                    # internal_error (the wire message exists but the question was
                    # withdrawn) — distinct from a plain delivery_failed.
                    if outcome is not None and outcome.get("compensated"):
                        return web.json_response({
                            "ok": False, "error": "internal_error",
                            "message": _ASK_INTERNAL_ERROR_MSG,
                        })
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
                    return web.json_response(
                        {"ok": False, "error": "delivery_failed"})
                return web.json_response({
                    "ok": True, "outcome": "anchored",
                    "question_number": number, "message_id": mid,
                })
            finally:
                # Terminal-failure BACKSTOP: clear the marker if this request
                # still owns it (CAS — a no-op when the poster already cleared it
                # at durable ownership, or a later ask claimed the marker).
                _clear_ask_marker(driver, eng_id, request_id)

        # --- BUTTON ask ---------------------------------------------------
        # Register the discrete-send INTENT (pending) at the ingress boundary for
        # idempotent transport-retry REATTACHMENT (§2(1)). The REAL relay-invoked
        # poster is installed just before we ARM (below) — posting is
        # RELAY-DEFERRED (§2, review C1): the relay posts the keyboard at the
        # ask's tool_use block, AFTER any preceding narration in the same frame.

        # Reserve the operator-message generation for the post-then-recheck race
        # AND the F-EXPIRE operator-away CAS. Sampled ONCE at entry, BEFORE any
        # BROKER.register, and stamped into the ask's static meta as
        # ``inbound_gen`` so both the main waiter and a same-request_id reattacher
        # (live request OR retired tombstone — both retain meta) read the SAME
        # generation for ``note_operator_away`` (Sol r2-2: a lost-response retry
        # reusing the FIRST attempt's generation can never re-wedge a cleared
        # away state with a fresher generation).
        gen_at_entry = (
            driver.inbound_generation(eng_id) if driver is not None else 0)

        def _ask_static_meta() -> dict:
            # F1 (Sol r3): the keyboard's STATIC metadata (options + topic_id +
            # operator_id + inbound_gen), seeded ATOMICALLY at broker creation.
            # The old code seeded meta AFTER register (``if created:
            # req.meta.update(...)``) ONLY on the main path, which lost the
            # metadata whenever a concurrent same-request_id RETRY created the
            # broker request first: the first attempt, suspended in number
            # allocation, resumed to find ``created=False`` and skipped the init,
            # leaving meta = {"message_id": ...} only ⇒ every tap rejected
            # (topic_id/operator_id both absent). Now BOTH the reattach path and
            # the main path pass ``meta=`` to ``register`` (a single synchronous
            # op — register only seeds meta on creation, with no await between),
            # so whichever call wins the create race installs the complete static
            # metadata.
            return {
                "options": options,
                "topic_id": rec.topic_id,
                "operator_id": rec.origin.get("user_id"),
                "inbound_gen": gen_at_entry,
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
                    # Finding 1: a prior operator-away refusal recorded an
                    # ``operator_away`` outcome on the intent → return it verbatim
                    # BEFORE touching the broker (reattach-outcome check → away
                    # gate → broker). Without this, the retry re-registers a fresh
                    # broker request and burns the full timeout with no keyboard.
                    prior = driver.send_intent_outcome(eng_id, request_id)
                    if _refused_intent_outcome(prior):
                        return web.json_response(prior)
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
                    return await _ask_final_response(
                        outcome, options, driver, eng_id, request_id, req)
                intent_registered = True

        # F-EXPIRE (§A2.4) GATE: while operator-away, refuse a genuinely-new ask
        # immediately — no broker request, no keyboard, no timeout burn. Placed
        # AFTER the reattach check so a transport retry of an already in-flight
        # ask still reattaches to its live/tombstoned outcome above.
        if _operator_away_active(driver, eng_id):
            if intent_registered:
                # Finding 1: record the refusal OUTCOME (not a bare cancel) so a
                # same-id retry reattaches to it above.
                _record_intent_refusal(driver, eng_id, request_id)
            return _away_refusal_response(driver, eng_id)

        # INBOUND GATE (§4): an unseen operator message means "end your turn". A
        # registered intent records the unread_inbound OUTCOME (Sol A2 wave-3,
        # Finding 3 — NOT a bare cancel) so a same-request_id retry reattaches to
        # it and returns unread_inbound immediately instead of delivery_failed.
        if driver is not None and driver.inbound_unread_depth(eng_id) > 0:
            return _refusal_response(record_intent=intent_registered)

        # §A3(c) INGRESS RESERVATION (Sol r2-8/r3-6): atomically CHECK the live-
        # pending predicate + CLAIM the ``ask_inflight`` marker under the ask-
        # maintenance lock (held for the check + marker ONLY). A second concurrent
        # ask sees it and refuses ``question_pending``. The marker clears at
        # BROKER.register below (durable ownership — the broker-pending clause
        # takes over gap-free) and on the allocation-failure path.
        _btn_lock = _ask_maint_lock(driver, eng_id)
        if _btn_lock is not None:
            async with _btn_lock:
                if _ask_pending_predicate(
                        driver, eng_id, exclude_request_id=request_id):
                    if intent_registered:
                        driver.cancel_send_intent(eng_id, request_id)
                    return _ask_pending_response(driver, eng_id)
                driver.set_ask_inflight(eng_id, request_id)

        # A RAISING allocator (Sol r8-4) is TERMINAL BEFORE any wire post — clear
        # the marker, tombstone the intent (internal_error outcome; retries
        # short-circuit), refuse. An ABSENT allocator returns None (un-numbered
        # legacy path).
        try:
            number = await _maybe_allocate_number(engagement_registry, eng_id)
        except Exception:  # noqa: BLE001
            logger.warning("button number allocation failed (eng=%s)",
                           eng_id[:8], exc_info=True)
            _clear_ask_marker(driver, eng_id, request_id)
            if intent_registered:
                _record_intent_internal_error(driver, eng_id, request_id)
            return web.json_response({"ok": False, "error": "internal_error"})
        # W-R3 (Sol r1-5): the SINGLE canonical body — full options VERBATIM,
        # numbered, below the question. This exact string feeds the keyboard
        # post, the persisted ``open_questions[].text``, the finish-hook settle
        # base, and (via the persisted text) boot reconciliation.
        display = render_ask_body(number, question, options)

        # F1: create-with-metadata atomically (STATIC meta seeded at creation so
        # a fast tap never sees incomplete metadata — r3-B3 fast-tap — AND a
        # concurrent reattach that created the request first still finds it
        # complete). message_id + finish_hook are set later by the broker-owned
        # setup task (r8-B3). ``meta`` is ignored if the request already exists.
        req, _created = BROKER.register(
            namespace="engagement_ask", scope=eng_id, request_id=request_id,
            timeout_s=timeout_s, meta=_ask_static_meta(),
        )
        # §A3(c): durable ownership reached — the request is live in the broker
        # (BROKER.pending non-empty), so the ingress marker clears SYNCHRONOUSLY
        # here (no maintenance lock) and the broker-pending clause of the gate
        # predicate takes over gap-free.
        _clear_ask_marker(driver, eng_id, request_id)

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
        # Pass ``shorts`` to the keyboard ONLY when at least one option carried an
        # agent short — str-only asks keep today's call shape (backward-compatible
        # with existing keyboard fakes that don't accept the kwarg).
        _kbd_kwargs = {"shorts": shorts} if any(shorts) else {}

        async def _post_ask() -> int | None:
            try:
                await BROKER.ensure_posted(
                    req,
                    lambda: telegram_channel.post_options_keyboard(
                        engagement_id=eng_id, request_id=request_id,
                        question=display, options=options, **_kbd_kwargs),
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
        return await _ask_final_response(
            outcome, options, driver, eng_id, request_id, req)

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


async def _withdraw_anchor(
    telegram_channel: Any, topic_id: int | None, mid: int,
) -> None:
    """§A3(c) compensation: best-effort confirmed WITHDRAW-edit of an orphan
    anchor (posted, but its ledger write failed) via the RAW wire edit primitive
    — NEVER ``edit_discrete`` (the initial-anchor poster runs under the sequencer
    lock on the relay task; no reacquisition from poster context). An unconfirmed
    edit leaves one stale plain-text line — the documented visual-orphan class."""
    try:
        confirmed = await confirmed_settle_edit(
            lambda: telegram_channel.edit_topic_message(
                topic_id, mid, _ANCHOR_WITHDRAWN, clear_keyboard=True),
        )
        if not confirmed:
            logger.warning(
                "anchor withdraw edit UNCONFIRMED (topic=%s mid=%s) — stale "
                "plain-text orphan left", topic_id, mid)
    except Exception:  # noqa: BLE001 — compensation edit is best-effort
        logger.debug("anchor withdraw edit raised", exc_info=True)


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
