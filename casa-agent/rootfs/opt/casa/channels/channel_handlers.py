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
        try:
            msg_id = await telegram_channel.send_response_to_topic(topic_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "send_to_topic failed for engagement=%s topic=%s: %s",
                engagement_id, topic_id, exc,
            )
            return web.json_response({"ok": False, "error": "send_failed"})

        # W2/Sol B9 (Task 7): the agent's first outbound reply flips
        # first_contact_required -> awaiting_operator. getattr-tolerant —
        # a fake registry in a test may not carry the method; a no-op
        # returns None for non-interaction-required engagements.
        advance = getattr(engagement_registry, "advance_interaction_state", None)
        if advance is not None:
            await advance(engagement_id, "first_contact")

        if record_reply is not None and text:
            try:
                record_reply(engagement_id, text)
            except Exception:  # noqa: BLE001 — de-dup hint is best-effort
                logger.debug("record_reply hook failed", exc_info=True)

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


def _validate_ask_args(body: dict) -> tuple[str, list, float] | None:
    """Validate + clamp the `ask` request body.

    Returns ``(question, options, clamped_timeout_s)`` on success, or
    ``None`` on any validation failure (caller maps to ``invalid_args``).
    """
    question = body.get("question")
    if (not isinstance(question, str) or not question
            or len(question) > _ASK_MAX_QUESTION_LEN):
        return None
    options = body.get("options")
    if (not isinstance(options, list)
            or not (_ASK_MIN_OPTIONS <= len(options) <= _ASK_MAX_OPTIONS)):
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


def _ask_keyboard_finish(
    telegram_channel: Any, topic_id: int | None, message_id: int,
    question: str, options: list,
) -> Callable[[dict], "Awaitable[None]"]:
    """Broker finish-hook (r3-B3 shape, mirrors ``hooks._perm_keyboard_finish``)
    -- the engagement_ask namespace's ONLY keyboard-message writer. Fires
    exactly once on outcome (delivered by the broker even if the posting
    HTTP handler was cancelled/disconnected) and edits the posted question
    message to show the resolution. ``_on_inline_callback`` never edits the
    message itself -- it only ``cq.answer()``s.
    """

    async def _finish(outcome: dict) -> None:
        o = outcome.get("outcome")
        if o == "answered":
            idx = outcome.get("option_index")
            label = (
                options[idx]
                if isinstance(idx, int) and 0 <= idx < len(options)
                else "?"
            )
            text = f"{question}\n\n✅ Answered: {label}"
        else:
            text = f"{question}\n\n⌛ Expired"
        try:
            await telegram_channel.edit_topic_message(topic_id, message_id, text)
        except Exception:  # noqa: BLE001 — finish hooks must never raise
            logger.warning(
                "ask keyboard finish-hook edit failed "
                "(topic=%s message_id=%s)", topic_id, message_id, exc_info=True,
            )

    return _finish


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

        # W2/Sol B9 (Task 7): asking is an outbound agent action too — the
        # FIRST reply/ask flips first_contact_required -> awaiting_operator.
        # getattr-tolerant (see _make_send_to_topic); a no-op past the first
        # call (already awaiting_operator/authorized) or for a
        # non-interaction-required engagement ("").
        advance = getattr(engagement_registry, "advance_interaction_state", None)
        if advance is not None:
            await advance(eng_id, "first_contact")

        req, created = BROKER.register(
            namespace="engagement_ask", scope=eng_id, request_id=request_id,
            timeout_s=timeout_s,
        )
        if created:
            # STATIC meta BEFORE posting so a fast tap never sees incomplete
            # metadata (r3-B3 fast-tap). message_id + finish_hook are set by
            # the broker-owned setup task (r8-B3).
            req.meta.update({
                "options": options,
                "topic_id": rec.topic_id,
                "operator_id": rec.origin.get("user_id"),
            })

        await BROKER.ensure_posted(
            req,
            lambda: telegram_channel.post_options_keyboard(
                engagement_id=eng_id, request_id=request_id,
                question=question, options=options),
            lambda mid: _ask_keyboard_finish(
                telegram_channel, rec.topic_id, mid, question, options),
        )
        # Shielded future: a CancelledError here (transport disconnect)
        # propagates to OUR caller without cancelling the broker's shared
        # future -- the request stays live for a same-id reattach.
        outcome = await BROKER.await_result(req)

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
            return web.json_response({"ok": False, "error": "cancelled"})
        # delivery_failed (keyboard post raised or returned None, r10-B3).
        return web.json_response({"ok": False, "error": "delivery_failed"})

    return handler


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
