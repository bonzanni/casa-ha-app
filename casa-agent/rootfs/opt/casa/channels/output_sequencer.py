"""Per-topic OUTPUT SEQUENCER + relay-mediated discrete-posting intent registry
(v0.79.0 Primitive A — engagement-topic UX; design §2, Sol r1-1/2/3, r11-1
consolidation, r12/r13).

This module owns the machinery that makes an engagement topic's message order a
property of a single serialized writer rather than of racing coroutines:

* :class:`OutputSequencer` — ONE serialization lock + ONE background intent
  watcher task per ``claude_code`` engagement topic. It is the ONLY writer to
  the topic: narration posts/edits (driven by ``drivers.topic_stream``), reply-
  tool sends, ask/permission keyboards, platform notices and summary edits all
  funnel through it. It owns the topic HIGH-WATER MARK (the newest
  sequencer-posted message id) and the per-message no-op edit cache (F1).

* :class:`IntentRegistry` — the relay-mediated discrete-posting state (§2, bullet
  RELAY-MEDIATED DISCRETE POSTING). A discrete send does NOT post itself: its
  ingress registers a :class:`SendIntent` (``pending``), arms it at the point of
  no return (``armed``), or cancels it (``cancelled`` → TOMBSTONE). The relay,
  processing the subprocess event stream in the ONE true causal order, matches
  intents at CONTENT-BLOCK positions and posts armed intents through the
  sequencer at their block. Late/absent intents post out of exact position via
  the 2s ordered-slot hold and the 10s intent timeout.

Concurrency note (deviation disclosed for T1 review): §2 phrases the serializer
as "one asyncio.Task + queue". This module realizes the SAME single-writer
invariant with an :class:`asyncio.Lock` guarding every post + high-water/narration
mutation, plus ONE background task (:meth:`OutputSequencer.run_watcher`) that
drives late/timeout discrete posts. A lock (rather than a literal queue) was
chosen so ``drivers.topic_stream``'s crash-safe cursor/checkpoint contract —
which the design mandates be PRESERVED — keeps its synchronous
post-then-checkpoint shape (the relay ``await``s a sequencer op, learns the
message id, then checkpoints). The observable §2 contract (ordering, sealing,
rollover, intent states, slot hold, timeout + consumption debt, reattachment,
no-op edit gate, de-dup-before-post) is identical either way.

Clocks are injectable (``_now`` / ``_sleep``); no code here patches the global
``asyncio.sleep`` (the module-local / injected-clock rule, CLAUDE.md memory
cage).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from authz_grants import canonical_args_hash

logger = logging.getLogger(__name__)

# -- tunables (module-local so tests can shrink them without touching the
#    process-wide asyncio module) --------------------------------------------
_SLOT_HOLD_S = 2.0        # §2(4): ordered-slot hold — wait for a pending/absent
#                           intent to arm/cancel before proceeding past a block.
_INTENT_TIMEOUT_S = 10.0  # §2(5): an armed intent unmatched by any block for
#                           this long posts out-of-band with a WARN + a debt.
_HOLD_POLL_S = 0.05       # slot-hold re-check cadence (the happy path arms well
#                           inside one poll; MCP calls land in ms).
_DISCRETE_CACHE_CAP = 64  # A9 (Sol r2-10b): bounded FIFO of discrete
#                           post_discrete/edit_discrete no-op-cache keys, so
#                           keyboard entries don't accumulate for the
#                           engagement's lifetime. Eviction past the cap drops
#                           the oldest DISCRETE _edit_cache entry (narration /
#                           summary entries are never in this FIFO, so they are
#                           untouched); the no-op gate then re-edits once.

# Canonical channel-MCP tool names (the ask/reply ingresses — §2 pinned
# ingress (a)). ``HOLD_ELIGIBLE_TOOLS`` is the set of tool kinds that ALWAYS
# correspond to a discrete post, so a block for one holds a slot even when no
# intent is registered yet. T2/T3 extend this set for emit_completion; permission
# keyboards fence on the GATED tool's own frame and are recognized reactively
# (only when a matching intent/tombstone already exists), never held blindly.
ASK_TOOL = "mcp__casa-engagement-channel__ask"
REPLY_TOOL = "mcp__casa-engagement-channel__reply"
# emit_completion is the svc_casa_mcp ingress (§2 pinned ingress (c)); its frame
# is hold-eligible so the completion post never overtakes a still-pending block.
EMIT_COMPLETION_TOOL = "mcp__casa-framework__emit_completion"
HOLD_ELIGIBLE_TOOLS: frozenset[str] = frozenset(
    {ASK_TOOL, REPLY_TOOL, EMIT_COMPLETION_TOOL})


# ---------------------------------------------------------------------------
# Pinned projection → hash (§2, "Hash identity"): computed AT THE INGRESS
# BOUNDARY from RAW args under pinned projections, using the v0.76 canonical
# helper. The SAME function is applied by the relay to each tool_use block, so
# an intent's transmitted hash and the block's computed hash agree.
# ---------------------------------------------------------------------------


def project_args(tool_name: str, raw_args: dict) -> dict:
    """Apply the pinned projection for *tool_name* to *raw_args*.

    * ``ask`` → ``{question, options, timeout_s-as-given, multi-as-given}``.
    * ``reply`` → ``{text}`` (drops the SDK-compat ``chat_id``).
    * everything else (a permission-gated tool's own frame, ``emit_completion``)
      → identity over the raw args.

    A5 · F-MULTI (v0.83.0): ``multi`` joins the ask projection — this MUST stay
    byte-identical to ``casa_engagement_channel._ask_projection_hash`` (the
    client side), or a multi ask's relay intent would never match its block.
    """
    if not isinstance(raw_args, dict):
        raw_args = {}
    if tool_name == ASK_TOOL:
        return {
            "question": raw_args.get("question"),
            "options": raw_args.get("options"),
            "timeout_s": raw_args.get("timeout_s"),
            "multi": raw_args.get("multi", False),
        }
    if tool_name == REPLY_TOOL:
        return {"text": raw_args.get("text")}
    return dict(raw_args)


def projection_hash(tool_name: str, raw_args: dict) -> str:
    """``canonical_args_hash`` of the pinned projection of *raw_args*."""
    return canonical_args_hash(project_args(tool_name, raw_args))


# ---------------------------------------------------------------------------
# Intent registry.
# ---------------------------------------------------------------------------


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class SendIntent:
    """One relay-mediated discrete-posting intent (§2, RELAY-MEDIATED DISCRETE
    POSTING).

    ``state`` walks ``pending → armed → posted|cancelled`` (§2(2)). A
    ``cancelled`` intent is a TOMBSTONE. ``timeout_posted`` marks the one-block
    CONSUMPTION DEBT left by the 10s-timeout out-of-band post (§2(5)):
    ``state`` is ``posted`` but the item stays MATCHABLE so its late-arriving
    block is consumed silently. ``consumed`` retires the item from matching
    once a block has bound (or consumed-cancelled / debt-consumed) it.
    """

    request_id: str
    tool_name: str
    projection_hash: str
    poster: Any                     # Callable[[], Awaitable[int|None]] | str
    registered_at: float
    seq: int
    state: str = "pending"          # pending | armed | posted | cancelled
    message_id: int | None = None
    outcome: dict | None = None
    slot_missed: bool = False       # relay slot timed out while this was pending
    timeout_posted: bool = False    # §2(5) one-block consumption debt
    consumed: bool = False          # retired from matching
    post_failed: bool = False       # F3: poster failed — surfaced ok:false,
    #                                 retired from matching (NOT a success debt)

    # -- matchability predicates -------------------------------------------
    def matchable(self) -> bool:
        """Still eligible to bind a content-block (§2(3)).

        Pending/armed intents, un-consumed cancelled tombstones, and the
        one-block timeout-posted debt are matchable; a consumed or
        post-failed (F3) item is not.
        """
        if self.consumed or self.post_failed:
            return False
        if self.state in ("pending", "armed", "cancelled"):
            return True
        return self.state == "posted" and self.timeout_posted


class IntentRegistry:
    """Ordered per-engagement intent + tombstone store (§2(1)-(3),(6)).

    Registration order is total (``seq``); matching a content-block always
    picks the OLDEST matchable item with an equal ``(tool_name,
    projection_hash)`` — FIFO on equal hashes on both sides. The ``request_id``
    → intent map gives idempotent transport-retry REATTACHMENT (§2(1)); a
    retry whose id matches an existing intent returns that intent (and its
    recorded outcome) rather than creating a second one.
    """

    def __init__(self, *, _now: Callable[[], float] = time.monotonic) -> None:
        self._now = _now
        self._by_seq: list[SendIntent] = []
        self._by_request: dict[str, SendIntent] = {}
        self._next_seq = 0

    def by_request_id(self, request_id: str) -> SendIntent | None:
        return self._by_request.get(request_id)

    def register(
        self, *, request_id: str, tool_name: str, projection_hash: str, poster: Any,
    ) -> tuple[SendIntent, bool]:
        """Register (or REATTACH to) an intent. Returns ``(intent, created)``.

        A same-``request_id`` call REATTACHES idempotently (§2(1)): the existing
        intent is returned with ``created=False`` so a transport retry can read
        the recorded outcome (including the posted ``message_id``) and can
        neither double-post nor consume another frame.
        """
        existing = self._by_request.get(request_id)
        if existing is not None:
            return existing, False
        intent = SendIntent(
            request_id=request_id,
            tool_name=tool_name,
            projection_hash=projection_hash,
            poster=poster,
            registered_at=self._now(),
            seq=self._next_seq,
        )
        self._next_seq += 1
        self._by_seq.append(intent)
        self._by_request[request_id] = intent
        return intent, True

    def set_poster(self, request_id: str, poster: Any) -> SendIntent | None:
        """Replace the intent's poster (T3: the ingress registers early for
        idempotent reattach detection, then installs the REAL relay-invoked
        poster before arming). No-op if the intent is gone."""
        intent = self._by_request.get(request_id)
        if intent is not None:
            intent.poster = poster
        return intent

    def arm(self, request_id: str) -> SendIntent | None:
        intent = self._by_request.get(request_id)
        if intent is not None and intent.state == "pending":
            intent.state = "armed"
        return intent

    def cancel(self, request_id: str) -> SendIntent | None:
        """Cancel a pending/armed intent → TOMBSTONE (§2(2))."""
        intent = self._by_request.get(request_id)
        if intent is not None and intent.state in ("pending", "armed"):
            intent.state = "cancelled"
        return intent

    def oldest_matchable(
        self, tool_name: str, projection_hash: str,
    ) -> SendIntent | None:
        for intent in self._by_seq:
            if (
                intent.matchable()
                and intent.tool_name == tool_name
                and intent.projection_hash == projection_hash
            ):
                return intent
        return None

    def armed_unposted(self) -> list[SendIntent]:
        return [
            i for i in self._by_seq
            if i.state == "armed" and not i.consumed and not i.post_failed
            and i.message_id is None
        ]

    def has_any_matchable(self) -> bool:
        """True iff any intent/tombstone is still matchable — i.e. a discrete
        ingress is currently active for this engagement. Used to keep the slot
        hold DORMANT while no ingress has registered anything (the T1-stubbed
        state and any quiescent turn), so a hold-eligible tool_use block never
        stalls narration when there is provably nothing to wait for."""
        return any(i.matchable() for i in self._by_seq)

    def prune(self) -> None:
        """§2(6): drop all intents, tombstones and id→outcome at turn end."""
        self._by_seq.clear()
        self._by_request.clear()


# ---------------------------------------------------------------------------
# No-op edit gate (F1): tri-state markup.
# ---------------------------------------------------------------------------

MARKUP_ABSENT = "absent"          # no reply_markup touched
MARKUP_EMPTY = "empty"            # explicit-empty (clear keyboard) — T3
_ABSENT = object()                # sentinel: caller passed no markup argument


def _markup_tristate(markup: Any) -> Any:
    """Map a markup argument to its tri-state cache key (F1, Sol r2-2).

    ``absent`` (no markup touched) | ``empty`` (explicit clear) |
    ``non-empty`` serialized to a stable ``str`` so presence-alone can never
    suppress a markup-only settlement.
    """
    if markup is _ABSENT or markup is None:
        return MARKUP_ABSENT
    if isinstance(markup, str) and markup == MARKUP_EMPTY:
        return MARKUP_EMPTY
    return f"markup:{markup!r}"


# Result codes from edit_narration_if_latest / post_for_block.
APPLIED = "applied"
SEALED = "sealed"
FAILED = "failed"


# ---------------------------------------------------------------------------
# The sequencer.
# ---------------------------------------------------------------------------

SendMessage = Callable[[int, str], Awaitable[int | None]]
EditMessage = Callable[[int, int, str], Awaitable[bool]]
# A9 markup-capable wire primitives (injected by the driver's _relay_* wrappers;
# production always supplies them, tests inject fakes). ``send_message_markup``
# posts plain text + an inline keyboard and returns the message id;
# ``edit_message_markup`` edits text and/or markup (``text=None`` ⇒ markup-only;
# ``markup is _ABSENT`` ⇒ leave the keyboard untouched).
SendMessageMarkup = Callable[..., Awaitable[int | None]]
EditMessageMarkup = Callable[..., Awaitable[bool]]


class OutputSequencer:
    """Serialized single-writer for ONE engagement topic (§2).

    Injected primitives ``send_message(topic_id, text) -> msg_id|None`` and
    ``edit_message(topic_id, msg_id, text) -> bool`` keep it unit-testable in
    isolation (mirrors ``drivers.topic_stream``'s style).
    """

    def __init__(
        self,
        *,
        engagement_id: str,
        topic_id: int,
        send_message: SendMessage,
        edit_message: EditMessage,
        send_message_markup: SendMessageMarkup | None = None,
        edit_message_markup: EditMessageMarkup | None = None,
        _now: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], Awaitable[None]] | None = None,
        slot_hold_s: float = _SLOT_HOLD_S,
        intent_timeout_s: float = _INTENT_TIMEOUT_S,
        hold_poll_s: float = _HOLD_POLL_S,
    ) -> None:
        self.engagement_id = engagement_id
        self.topic_id = topic_id
        self.send_message = send_message
        self.edit_message = edit_message
        # A9: markup-capable wire primitives. Default None keeps the sequencer
        # constructible without them; post_discrete/edit_discrete raise a clear
        # RuntimeError if used un-injected (belt-and-suspenders — production
        # always injects via the driver's _ensure_sequencer wiring).
        self._send_message_markup = send_message_markup
        self._edit_message_markup = edit_message_markup
        self._now = _now
        self._sleep = _sleep or _default_sleep
        self._slot_hold_s = slot_hold_s
        self._intent_timeout_s = intent_timeout_s
        self._hold_poll_s = hold_poll_s

        self._lock = asyncio.Lock()
        # REENTRANT-PER-TASK ownership (Sol diff gate r2). The task currently
        # inside the serialization lock, or None. A poster the sequencer awaits
        # WHILE holding the lock (seal-narration + post is atomic) may call back
        # into ``edit_summary`` — e.g. the ask poster's ``note_ask_waiting`` →
        # SummaryController.submit_status → edit_summary — on this SAME task; a
        # plain non-reentrant ``asyncio.Lock`` would deadlock it forever. Owner
        # tracking lets that nested, already-serialized call proceed.
        self._lock_owner: asyncio.Task | None = None
        self.registry = IntentRegistry(_now=_now)
        # F3 fail-closed posting: per-request resolution events. A deferred
        # ask/reply/anchor handler AWAITS the intent's outcome (posted ok, or
        # poster-failure ok:false) bounded by ``post_await_budget`` and returns
        # ok only when the post actually landed — an ``ok:true`` response with a
        # failed post is structurally impossible.
        self._resolution_events: dict[str, asyncio.Event] = {}
        # HIGH-WATER: newest sequencer-posted message id (the sequencer is the
        # only writer, so it is authoritative). ``_narration_msg_id`` is the
        # current OPEN narration message, or None when narration is SEALED.
        self._high_water: int | None = None
        self._narration_msg_id: int | None = None
        # F1 no-op edit gate: msg_id -> (text, markup_tristate).
        self._edit_cache: dict[int, tuple[Any, Any]] = {}
        # A9 (Sol r2-10b): bounded FIFO of DISCRETE-write cache keys only. Narration
        # entries retire on seal and summary entries live forever above the log;
        # discrete keyboard entries would otherwise leak, so post_discrete/
        # edit_discrete register their mids here and eviction past the cap drops
        # the oldest discrete _edit_cache entry (never a narration/summary one).
        self._discrete_cache_fifo: deque[int] = deque()
        self._arm_event = asyncio.Event()
        # v0.79.0 (§3, Primitive B): reply-threading. An inbound operator
        # envelope's delivery sets this to its Telegram message id; the turn's
        # FIRST sequencer-posted message (narration open, or an ask/reply
        # poster via ``consume_turn_reply_to``) threads to it, then clears it.
        self._turn_reply_to: int | None = None

    # -- serialization (reentrant-per-task; §2 one serialized writer) -------

    @asynccontextmanager
    async def _serialized(self):
        """Acquire the ONE serialization lock, REENTRANTLY for the task that
        already holds it (owner-tracked).

        INVARIANT (Sol diff gate r2): a locked section of the sequencer may,
        via a poster it awaits, call back into :meth:`edit_summary` (the
        NON-narration summary path). ``edit_summary`` must be reentrant-safe for
        the lock-OWNING task and must never block on the lock it is nested
        within. This is correct because the summary edit does NOT touch
        narration / high-water / open-narration state (see :meth:`edit_summary`'s
        docstring) — reentrant execution from within a narration-post critical
        section cannot corrupt narration invariants.

        Concretely: an ask poster runs while the writer lock is held (seal-
        narration + post is atomic — see :meth:`_post_intent_locked`). That
        poster calls ``driver.note_ask_waiting`` → SummaryController.submit_status
        → ``edit_summary``, which re-enters here on the SAME task. A plain
        non-reentrant ``asyncio.Lock`` would deadlock that task forever.

        Reentrancy is safe because asyncio is single-threaded: the reentrant
        body only runs when the SAME task already holds the lock, so no
        concurrent mutation is possible. This keeps §2's single-writer
        invariant (one lock, not a second summary lock) intact — a DIFFERENT
        task still contends on ``self._lock`` and is fully serialized.
        """
        if self._lock_owner is asyncio.current_task():
            # Already serialized by this very task — reenter without re-acquiring.
            yield
            return
        async with self._lock:
            self._lock_owner = asyncio.current_task()
            try:
                yield
            finally:
                self._lock_owner = None

    def serialized(self):
        """PUBLIC alias of :meth:`_serialized` — the reentrant-per-task writer CM.

        GLOBAL LOCK-ORDER (Sol diff gate r3): the sequencer writer lock is the
        OUTER lock in the one sanctioned order ``sequencer → summary``.
        :class:`drivers.summary_controller.SummaryController` acquires THIS
        (reentrantly, if the caller — e.g. an armed ask poster the sequencer
        awaits under the held writer lock — already owns it) BEFORE its own
        summary lock, so *no code ever holds the summary lock while acquiring the
        sequencer lock*. That removes the former summary→sequencer ordering
        entirely, making an AB-BA cross-task cycle impossible: because holding the
        summary lock now REQUIRES first holding this writer lock, and only one task
        holds it at a time, a second task can never hold the summary lock while the
        first holds the sequencer lock.

        Returns the SAME reentrant-per-task context manager as ``_serialized``;
        internal callers keep using ``_serialized`` unchanged.
        """
        return self._serialized()

    # -- narration ----------------------------------------------------------

    @property
    def narration_msg_id(self) -> int | None:
        return self._narration_msg_id

    @property
    def high_water(self) -> int | None:
        return self._high_water

    async def open_narration(self, text: str) -> int | None:
        """Post a NEW narration message; it becomes the open narration and the
        high-water mark."""
        async with self._serialized():
            return await self._open_narration_locked(text)

    async def _open_narration_locked(self, text: str) -> int | None:
        # v0.79.0 (§3): thread the turn's FIRST post to the inbound envelope
        # that triggered the turn (reply-quoting). Consumed once — a later post
        # this turn is not a reply to the operator's message.
        reply_to = self._turn_reply_to
        self._turn_reply_to = None
        if reply_to is not None:
            mid = await _maybe_await(
                self.send_message(self.topic_id, text, reply_to=reply_to))
        else:
            mid = await _maybe_await(self.send_message(self.topic_id, text))
        if mid is None:
            return None
        self._high_water = mid
        self._narration_msg_id = mid
        self._edit_cache[mid] = (text, MARKUP_ABSENT)
        return mid

    async def edit_narration_if_latest(
        self, msg_id: int, text: str, *, markup: Any = _ABSENT,
    ) -> str:
        """Edit *msg_id* IFF it is still the newest sequencer-posted message
        (§2). Otherwise return :data:`SEALED` — the caller opens a fresh
        narration message for the pending text.

        Routes through the F1 no-op edit gate: an identical (text, markup)
        edit is skipped (returns :data:`APPLIED`); a FAILED edit invalidates
        the cache entry so a retry is never suppressed.
        """
        async with self._serialized():
            if msg_id != self._narration_msg_id or msg_id != self._high_water:
                return SEALED
            tri = _markup_tristate(markup)
            if self._edit_cache.get(msg_id) == (text, tri):
                return APPLIED  # no-op skip
            ok = await _maybe_await(self.edit_message(self.topic_id, msg_id, text))
            if not ok:
                self._edit_cache.pop(msg_id, None)  # invalidate → retry allowed
                return FAILED
            self._edit_cache[msg_id] = (text, tri)
            return APPLIED

    async def edit_summary(self, msg_id: int, text: str) -> str:
        """Edit the pinned live SUMMARY message (§5 — the R1 exception).

        The summary is the FIRST topic message and lives ABOVE the append-only
        causal log; its edits are NON-narration. Unlike
        :meth:`edit_narration_if_latest`, this path deliberately does NOT touch
        the narration/high-water invariants: it never seals the open narration,
        never advances the high-water mark, and the summary message is never
        itself sealed. That keeps the summary a living message while the causal
        log below it stays strictly append-only (T1 invariants intact — the
        summary id is posted before any narration, so it is never the
        high-water/open-narration id).

        Still funnels through the single serialization lock (one writer) and the
        F1 no-op edit gate: an identical edit skips (returns :data:`APPLIED`); a
        FAILED edit invalidates the cache entry so a retry is never suppressed.
        """
        async with self._serialized():
            if self._edit_cache.get(msg_id) == (text, MARKUP_ABSENT):
                return APPLIED
            ok = await _maybe_await(self.edit_message(self.topic_id, msg_id, text))
            if not ok:
                self._edit_cache.pop(msg_id, None)
                return FAILED
            self._edit_cache[msg_id] = (text, MARKUP_ABSENT)
            return APPLIED

    async def seal_narration(self) -> None:
        """Explicitly SEAL the open narration (nothing edits it again)."""
        async with self._serialized():
            self._seal_narration_locked()

    def _seal_narration_locked(self) -> None:
        """SEAL the open narration and drop its now-dead no-op edit-cache entry
        (a sealed message is never edited again — its cache key is unreachable,
        so retaining it only grows the cache unbounded)."""
        if self._narration_msg_id is not None:
            self._edit_cache.pop(self._narration_msg_id, None)
        self._narration_msg_id = None

    async def advance_high_water_for_inbound(
        self, operator_msg_id: int | None = None,
    ) -> None:
        """§2: an inbound operator message is a causal event — it advances the
        high-water mark at handler entry and SEALS open narration.

        (The inbound-spool call site is wired by T2; this is the machinery it
        invokes.)
        """
        async with self._serialized():
            self._seal_narration_locked()
            if operator_msg_id is not None:
                if self._high_water is None or operator_msg_id > self._high_water:
                    self._high_water = operator_msg_id

    # -- reply-threading (v0.79.0 §3, Primitive B) -------------------------

    def set_turn_reply_to(self, message_id: int | None) -> None:
        """Record the inbound operator message id that the turn's FIRST
        sequencer post should reply-thread to (§3). Overwrites any prior
        un-consumed value — the most-recently delivered envelope wins."""
        self._turn_reply_to = message_id

    def consume_turn_reply_to(self) -> int | None:
        """Return and CLEAR the pending reply-threading target (§3).

        Used by ask/reply posters (T3) so whichever output posts first this
        turn — narration open or a discrete send — threads to the operator's
        message and the rest do not."""
        mid = self._turn_reply_to
        self._turn_reply_to = None
        return mid

    # -- intent registration (the T2/T3 ingress API) -----------------------

    def register_intent(
        self, *, request_id: str, tool_name: str, projection_hash: str, poster: Any,
    ) -> tuple[SendIntent, bool]:
        """Register (or reattach to) a discrete-send intent (§2(1)). See
        :meth:`IntentRegistry.register`. Ingresses (T2/T3) call this at fence
        entry with a ``poster`` coroutine-factory that performs the actual
        keyboard/text post when the relay reaches the block."""
        return self.registry.register(
            request_id=request_id, tool_name=tool_name,
            projection_hash=projection_hash, poster=poster,
        )

    def set_intent_poster(self, request_id: str, poster: Any) -> SendIntent | None:
        """Install the REAL relay-invoked poster on a registered intent (§2(3),
        T3). The ask/reply ingress registers the intent early (for reattach
        idempotency), then sets this poster and ARMS — the relay invokes it when
        it reaches the intent's tool_use block."""
        return self.registry.set_poster(request_id, poster)

    def arm_intent(self, request_id: str) -> SendIntent | None:
        """Move a pending intent to ``armed`` — the point of no return (§2(2))."""
        intent = self.registry.arm(request_id)
        if intent is not None:
            self._arm_event.set()
        return intent

    def cancel_intent(self, request_id: str) -> SendIntent | None:
        """Cancel a pending/armed intent → tombstone (§2(2))."""
        intent = self.registry.cancel(request_id)
        if intent is not None:
            self._arm_event.set()
        return intent

    def intent_outcome(self, request_id: str) -> dict | None:
        """Recorded outcome for a reattaching retry (§2(1))."""
        intent = self.registry.by_request_id(request_id)
        return intent.outcome if intent is not None else None

    def record_intent_refusal(self, request_id: str, outcome: dict) -> SendIntent | None:
        """A2 (F-EXPIRE, Sol review Finding 1): the operator-away gate REFUSED
        this ask. Tombstone the intent (like :meth:`cancel_intent`) AND record a
        refusal OUTCOME so a same-``request_id`` transport retry REATTACHES to the
        SAME refusal rather than: awaiting a dead intent (→ ``delivery_failed``)
        or re-registering a fresh broker request (→ full timeout burn, no
        keyboard). Signals any fail-closed awaiter so a blocked
        :meth:`await_intent_resolution` unblocks with the refusal (never ok:true
        on a never-posted intent). No-op if the intent is unknown."""
        intent = self.registry.cancel(request_id)
        if intent is None:
            return None
        intent.outcome = dict(outcome)
        self._arm_event.set()
        self._signal_resolution(request_id)
        return intent

    async def mark_intent_posted(
        self, request_id: str, message_id: int | None,
    ) -> Any:
        """Record that a discrete ingress POSTED its message out-of-band (the
        keyboard/reply was sent eagerly by the handler rather than deferred to
        the relay's content-block). Marks the intent ``posted`` and leaves the
        §2(5) one-block CONSUMPTION DEBT (``timeout_posted``) so the relay
        silently consumes the matching tool_use block — sealing open narration
        at that position — instead of binding a later same-hash intent or
        emitting stray narration. Records the outcome (incl. ``message_id``) for
        response-loss-after-post retry reattachment (§2(1))."""
        async with self._serialized():
            intent = self.registry.by_request_id(request_id)
            if intent is None:
                return None
            intent.state = "posted"
            intent.consumed = False
            intent.timeout_posted = True
            intent.message_id = message_id
            intent.outcome = {
                "ok": message_id is not None,
                "message_id": message_id,
                "out_of_band": True,
            }
            if message_id is not None and (
                self._high_water is None or message_id > self._high_water
            ):
                self._high_water = message_id
            self._signal_resolution(request_id)
            return intent

    async def mark_intent_compensated(
        self, request_id: str, message_id: int,
    ) -> Any:
        """A3(c) COMPENSATED-INTENT path (Sol r5-5 + r6-1): account a physical
        wire message whose logical result is FAILURE.

        The A3 initial-anchor poster posts first, then strict-persists the
        ledger entry; on an ``add_open_question`` failure the message EXISTS on
        the wire but the ask must resolve ok:false. The poster best-effort
        edits the orphan to a withdrawn-copy via the RAW wire primitive (never
        ``edit_discrete`` — it runs under the sequencer lock on the relay task,
        no reacquisition) and calls this to reconcile the sequencer's causal
        accounting.

        Pinned invariants (spec §A3(c)): ``_high_water`` advances to the
        delivered *message_id* (so a later ask opens BELOW the orphan, never
        beside it), the intent resolves EXACTLY ONCE with ``{"ok": False,
        "message_id": message_id, "compensated": True}``, and ``post_failed`` is
        NOT separately re-fired.

        Divergence from :meth:`mark_intent_posted` (which records an ok success
        with a one-block ``timeout_posted`` debt): here the outcome is ok:false
        with a ``compensated`` marker, the intent is RETIRED from matching
        (``consumed=True``, so no relay/watcher re-fire and no phantom debt),
        and — unlike :meth:`_post_intent_locked`'s failure branch —
        ``post_failed`` stays False (the post is not a fail-closed miss; the
        message physically landed). Idempotent: a repeat call after
        compensation is a no-op (no double resolution / high-water re-advance)."""
        async with self._serialized():
            intent = self.registry.by_request_id(request_id)
            if intent is None:
                return None
            if intent.outcome is not None and intent.outcome.get("compensated"):
                return intent  # already compensated — exactly-once
            intent.state = "posted"
            intent.consumed = True          # retired from matching
            intent.timeout_posted = False   # no consumption debt (not a success)
            intent.post_failed = False      # NOT a fail-closed re-fire
            intent.message_id = message_id
            intent.outcome = {
                "ok": False, "message_id": message_id, "compensated": True,
            }
            if self._high_water is None or message_id > self._high_water:
                self._high_water = message_id
            self._signal_resolution(request_id)
            return intent

    # -- F3 fail-closed resolution await -----------------------------------

    def _signal_resolution(self, request_id: str) -> None:
        ev = self._resolution_events.get(request_id)
        if ev is not None:
            ev.set()

    @property
    def post_await_budget(self) -> float:
        """The bounded transport budget a deferred handler waits for a post to
        resolve (§4/T3-fix, F3): the slot hold plus the intent timeout plus a
        small margin so the 10s out-of-band watcher post is always covered."""
        return self._slot_hold_s + self._intent_timeout_s + 2.0

    async def await_intent_resolution(
        self, request_id: str, timeout: float | None = None,
    ) -> dict | None:
        """F3: block until intent *request_id* posts (ok) or fails (ok:false),
        bounded by *timeout* (defaults to :attr:`post_await_budget`). Returns the
        recorded outcome dict, or ``None`` if the intent is unknown or still
        unresolved at timeout (the caller treats a missing/failed outcome as
        ok:false — never ok:true). Edge-triggered: the outcome is checked under
        the lock BEFORE waiting, so a post that lands before the awaiter blocks
        is never missed."""
        if timeout is None:
            timeout = self.post_await_budget
        async with self._serialized():
            intent = self.registry.by_request_id(request_id)
            if intent is None:
                return None
            if intent.outcome is not None:
                return intent.outcome
            ev = self._resolution_events.get(request_id)
            if ev is None:
                ev = asyncio.Event()
                self._resolution_events[request_id] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        intent = self.registry.by_request_id(request_id)
        return intent.outcome if intent is not None else None

    async def await_completion_drain(
        self, request_id: str, timeout: float | None = None,
    ) -> bool:
        """F3: block until the relay CONSUMES the given consumption-debt intent
        (the emit_completion debt) — i.e. reaches its content block, having
        processed every PRIOR frame — so a completion post can never overtake
        lagging prior-frame narration.

        Bounded by *timeout* (default: the slot hold). Returns ``True`` when the
        debt has been consumed (or the intent is unknown / already consumed),
        ``False`` on timeout (the caller WARNs and proceeds — the ONE documented,
        bounded weakening). Edge-triggered: consumption is checked under the lock
        BEFORE waiting, so a consume that lands before the awaiter blocks is
        never missed."""
        if timeout is None:
            timeout = self._slot_hold_s
        async with self._serialized():
            intent = self.registry.by_request_id(request_id)
            if intent is None or intent.consumed:
                return True
            ev = self._resolution_events.get(request_id)
            if ev is None:
                ev = asyncio.Event()
                self._resolution_events[request_id] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        intent = self.registry.by_request_id(request_id)
        return intent is None or intent.consumed

    # -- F1(b) platform-origin discrete send -------------------------------

    async def post_platform_notice(
        self, text: str, *, reply_to: int | None = None,
    ) -> int | None:
        """F1(b): post a PLATFORM-ORIGIN discrete message (an inbound receipt /
        eviction / capacity notice) through the single writer.

        These have no subprocess frame, so they register no intent — but they
        MUST NOT post around the sequencer: a receipt slipping in below open
        narration while ``edit_narration_if_latest`` still returns APPLIED is
        exactly the ordering violation §2 forbids. So this seals open narration
        (the notice is a causal event below it), posts, and advances the
        high-water mark, all under the one serialization lock. Returns the posted
        message id, or ``None`` on send failure."""
        async with self._serialized():
            self._seal_narration_locked()
            if reply_to is not None:
                mid = await _maybe_await(
                    self.send_message(self.topic_id, text, reply_to=reply_to))
            else:
                mid = await _maybe_await(self.send_message(self.topic_id, text))
            if mid is not None and (
                self._high_water is None or mid > self._high_water
            ):
                self._high_water = mid
            return mid

    # -- A9 keyboard-bearing discrete writes (Sol r1-8) --------------------

    def _register_discrete_cache(self, mid: int) -> None:
        """Register *mid* in the bounded discrete-cache FIFO, evicting the
        oldest discrete ``_edit_cache`` entry past the cap.

        Re-registering an existing mid moves it to the tail (most-recent), so a
        repeatedly-edited keyboard is not evicted ahead of a stale one. Only
        entries created by :meth:`post_discrete`/:meth:`edit_discrete` are in
        this FIFO — narration (retired on seal) and summary (append-above)
        entries are never touched by eviction."""
        if mid in self._discrete_cache_fifo:
            self._discrete_cache_fifo.remove(mid)
        self._discrete_cache_fifo.append(mid)
        while len(self._discrete_cache_fifo) > _DISCRETE_CACHE_CAP:
            evicted = self._discrete_cache_fifo.popleft()
            self._edit_cache.pop(evicted, None)

    def _forget_discrete_cache(self, mid: int) -> None:
        """Drop *mid* from the discrete FIFO (a FAILED edit invalidated its
        cache entry, so it must not linger as a phantom FIFO slot)."""
        if mid in self._discrete_cache_fifo:
            self._discrete_cache_fifo.remove(mid)

    async def post_discrete(
        self, text: str, *, markup: Any = None, reply_to: int | None = None,
        revalidate: Any = None,
    ) -> int | None:
        """A9: post a keyboard-bearing DISCRETE message through the single writer
        (A3 anchor re-anchor). Mirrors :meth:`post_platform_notice` but sends via
        the markup-capable wire and maintains the F1 tri-state cache.

        Under the writer lock: run *revalidate* (sync or async — the A3
        answered/reserved final check) immediately before the send; a declined
        revalidation returns ``None`` with NO send and NO state change. On a
        successful send: SEAL open narration (the discrete message is a causal
        event below it), advance ``_high_water`` to the returned mid, seed the
        tri-state ``_edit_cache`` entry, and register the mid in the bounded
        discrete-cache FIFO. *reply_to* threads like the other sends.

        Deliberately NOT wrapped around ``ensure_posted`` posters (Sol r4-5): that
        runs its poster in a NEW task, which would deadlock against the
        relay-held, task-reentrant-only writer lock. Raises RuntimeError if the
        markup wire was not injected."""
        if self._send_message_markup is None:
            raise RuntimeError(
                "post_discrete requires an injected send_message_markup wire "
                "primitive (driver _ensure_sequencer wiring)")
        async with self._serialized():
            if revalidate is not None and not await _maybe_await(revalidate()):
                return None
            self._seal_narration_locked()
            mid = await _maybe_await(self._send_message_markup(
                self.topic_id, text, markup, reply_to=reply_to))
            if mid is None:
                return None
            if self._high_water is None or mid > self._high_water:
                self._high_water = mid
            self._edit_cache[mid] = (text, _markup_tristate(markup))
            self._register_discrete_cache(mid)
            return mid

    async def edit_discrete(
        self, msg_id: int, *, text: Any = None, markup: Any = _ABSENT,
        revalidate: Any = None,
    ) -> bool:
        """A9: markup-capable edit of a discrete message through the F1 tri-state
        no-op cache (A5 toggle redraw / multi settle edit).

        Touches NEITHER narration NOR high-water — it edits HISTORY, like
        :meth:`edit_summary`. ``text=None`` means a markup-only edit (a stable
        cache representation distinct from a text edit). Under the writer lock:
        run *revalidate* (the A5 terminal-race guard; declined → ``False``, no
        edit); F1 no-op gate — an identical ``(text, markup-tristate)`` returns
        ``True`` without any wire call; otherwise wire-edit and update the cache.

        **Returns ``bool``, deliberately NOT the APPLIED/FAILED string codes
        (Sol r2-10):** every settle path feeds ``confirmed_settle_edit``, whose
        gate is ``bool(await do_edit())`` — the string ``"failed"`` is truthy and
        would count a failed wire edit as CONFIRMED, deleting the recovery
        record. ``True`` ⇔ applied or no-op-skip; ``False`` ⇔ failed or
        revalidation-declined. Raises RuntimeError if the markup wire was not
        injected."""
        if self._edit_message_markup is None:
            raise RuntimeError(
                "edit_discrete requires an injected edit_message_markup wire "
                "primitive (driver _ensure_sequencer wiring)")
        async with self._serialized():
            if revalidate is not None and not await _maybe_await(revalidate()):
                return False
            tri = _markup_tristate(markup)
            if self._edit_cache.get(msg_id) == (text, tri):
                return True  # no-op skip — no wire call
            ok = await _maybe_await(self._edit_message_markup(
                self.topic_id, msg_id, text, markup))
            if not ok:
                self._edit_cache.pop(msg_id, None)   # invalidate → retry allowed
                self._forget_discrete_cache(msg_id)
                return False
            self._edit_cache[msg_id] = (text, tri)
            self._register_discrete_cache(msg_id)
            return True

    # -- F1(c) / F4 turn-boundary drain ------------------------------------

    async def flush_armed_intents(self) -> None:
        """F4: post every still-armed, un-posted intent out-of-band (WARN) so it
        RESOLVES before its registry entry is pruned. Turn-end pruning
        (topic_stream ``_finalize``) and finalize must never silently drop a
        late armed intent — a discrete send the agent believes is in flight
        would vanish and its awaiter would hang. Runs the same post path as the
        10s watcher; a failed post surfaces ok:false via F3."""
        async with self._serialized():
            for intent in self.registry.armed_unposted():
                await self._post_intent_locked(
                    intent, out_of_band=True, warn=True)

    def prune_turn(self) -> None:
        """§2(6): prune intents/tombstones/outcomes at turn end.

        Also CLEARS the causal-handoff one-shot reply anchor (§4): it "expires at
        turn end". A set-but-unconsumed anchor (a button answer that continued
        the turn but produced no output) must NOT leak into the next turn and
        mis-thread its first message.

        F3/F4: signal every outstanding resolution event before dropping them so
        an awaiter blocked past turn end unblocks (reading a resolved ok:false /
        ``None`` outcome) rather than hanging its transport budget out.

        NOTE (F6): ``topic_stream._finalize`` no longer calls this directly —
        it uses :meth:`drain_and_prune_turn`, which drains armed intents and
        prunes under ONE lock hold so a late-armed intent can't be dropped
        between a flush and this prune. This method stays for tests and any
        caller that needs a bare synchronous prune."""
        for ev in self._resolution_events.values():
            ev.set()
        self._resolution_events.clear()
        self.registry.prune()
        self._turn_reply_to = None

    async def drain_and_prune_turn(self) -> None:
        """F4+F6: atomically drain every still-armed intent, then prune + seal —
        under ONE lock hold.

        Replaces the former ``flush_armed_intents`` → ``prune_turn`` →
        ``seal_narration`` sequence in ``topic_stream._finalize``, which took and
        RELEASED the lock between those three steps. That gap let a late ingress
        register+arm an intent B during a flush poster-await AFTER the flush had
        snapshotted the armed set; ``prune_turn`` then deleted B before it could
        post (F6: intent B silently dropped, its awaiter left hanging / failing).

        Here the armed set is RE-SNAPSHOTTED under the held lock until it is
        empty, so every armed intent RESOLVES (posts out-of-band with a WARN, or
        fails closed via F3) first. There is NO await between the final
        empty-check and the synchronous prune, so a lock-free ``register_intent``
        /``arm_intent`` cannot slip an armed intent in between the two."""
        async with self._serialized():
            while True:
                pending = self.registry.armed_unposted()
                if not pending:
                    break
                for intent in pending:
                    await self._post_intent_locked(
                        intent, out_of_band=True, warn=True)
            # Registry is now stable (no await below until the clear).
            for ev in self._resolution_events.values():
                ev.set()
            self._resolution_events.clear()
            self.registry.prune()
            self._turn_reply_to = None
            self._seal_narration_locked()

    # -- discrete posting driven by the relay at a content-block ------------

    async def post_for_block(self, tool_name: str, block_hash: str) -> str:
        """Resolve a content-block position (§2(3)-(4)).

        Returns one of ``"posted"`` / ``"consumed_cancelled"`` /
        ``"debt_consumed"`` / ``"no_match"`` / ``"slot_timeout"``. Blocks:

        * armed intent  → seal narration, post at THIS position, mark posted;
        * cancelled tombstone → the block is consumed-cancelled (nothing posts);
        * timeout-posted debt → the block is consumed silently (§2(5));
        * pending intent OR absent-but-hold-eligible → HOLD up to the slot
          (§2(4)); on timeout mark a still-pending intent ``slot_missed`` and
          proceed (its late post happens out-of-band via the watcher).
        * absent and not hold-eligible → ``no_match`` immediately.
        """
        deadline = self._now() + self._slot_hold_s
        while True:
            async with self._serialized():
                resolved = await self._resolve_block_locked(tool_name, block_hash)
                if resolved is not None:
                    return resolved
            if self._now() >= deadline:
                async with self._serialized():
                    # Last-look under the lock: an intent that armed exactly at
                    # the deadline still posts at THIS block; a still-pending one
                    # is marked slot_missed (its late post happens out-of-band).
                    resolved = await self._resolve_block_locked(tool_name, block_hash)
                    if resolved is not None:
                        return resolved
                    item = self.registry.oldest_matchable(tool_name, block_hash)
                    if item is not None and item.state == "pending":
                        item.slot_missed = True
                return "slot_timeout"
            await self._sleep(self._hold_poll_s)

    async def _resolve_block_locked(
        self, tool_name: str, block_hash: str,
    ) -> str | None:
        """Resolve a content block against the registry (caller holds the lock).

        Returns a terminal result code, or ``None`` when the block must HOLD
        (a matching intent is still pending) — i.e. the caller keeps waiting.
        """
        item = self.registry.oldest_matchable(tool_name, block_hash)
        if item is not None:
            if item.state == "armed":
                await self._post_intent_locked(item, out_of_band=False)
                return "posted"
            if item.state == "cancelled":
                item.consumed = True
                return "consumed_cancelled"
            if item.state == "posted" and item.timeout_posted:
                # §2(5) debt consumed. SEAL open narration at this block's
                # position: the debt's message was posted out-of-band (the
                # timeout out-of-band post, or an eager ask/reply ingress post),
                # so narration up to this causal point must close — nothing may
                # edit/append to it below the discrete message.
                self._seal_narration_locked()
                item.consumed = True
                # F3: unblock a completion drain waiting for this debt — the
                # relay reaching the emit_completion block means every prior
                # frame has been processed.
                self._signal_resolution(item.request_id)
                return "debt_consumed"
            return None  # pending → HOLD
        if tool_name not in HOLD_ELIGIBLE_TOOLS:
            # Non-fenced tool: keep the fast path — never stall narration.
            return "no_match"
        # F4: hold-eligible block (ask/reply/emit_completion) with NO matching
        # intent yet. The empty-registry short-circuit that used to proceed here
        # DEFEATED the designed relay-first race: the discrete ingress (an MCP
        # call landing in milliseconds) may register its intent a beat AFTER the
        # relay reads this block. HOLD the 2s slot regardless of registry
        # emptiness; on slot timeout ``post_for_block`` proceeds and a genuinely
        # absent intent costs only the bounded hold.
        return None  # hold-eligible → HOLD for the slot

    async def _post_intent_locked(
        self, intent: SendIntent, *, out_of_band: bool, warn: bool = False,
    ) -> None:
        """Post *intent* (caller holds the lock). SEALS open narration first —
        rollover-on-interleave (§2, "narration seals when anything else posts
        below")."""
        self._seal_narration_locked()
        if warn:
            logger.warning(
                "output sequencer: intent %s (%s) unmatched by any block for "
                "%.0fs — posting out-of-band (engagement %s)",
                intent.request_id, intent.tool_name, self._intent_timeout_s,
                self.engagement_id,
            )
        try:
            if callable(intent.poster):
                mid = await _maybe_await(intent.poster())
            else:
                mid = await _maybe_await(
                    self.send_message(self.topic_id, str(intent.poster))
                )
        except Exception as exc:  # noqa: BLE001 — a poster failure must not wedge
            logger.warning(
                "output sequencer: intent %s poster failed: %s",
                intent.request_id, exc,
            )
            mid = None
        if mid is None:
            # §A3(c): the poster may have SELF-ACCOUNTED a compensated physical
            # write (initial-anchor add-failure): it posted the wire message,
            # then called ``mark_intent_compensated`` (reentrant under this lock)
            # which already resolved the intent ok:false+compensated and advanced
            # high-water, and returned None. Do NOT re-resolve as a plain
            # post-failure — that would clobber the mid + high-water accounting.
            if intent.outcome is not None and intent.outcome.get("compensated"):
                return
            # F3 fail-closed: the post did NOT land. Do NOT terminally consume
            # the intent as if it succeeded (no consumption debt claiming a
            # phantom post). Mark it post-failed (retired from matching so the
            # relay/watcher never silently re-fires it), record an ok:false
            # outcome, and resolve so the awaiting handler returns ok:false —
            # the agent learns the send failed instead of a swallowed error.
            intent.post_failed = True
            intent.outcome = {
                "ok": False, "message_id": None, "out_of_band": out_of_band,
            }
            self._signal_resolution(intent.request_id)
            return
        intent.state = "posted"
        intent.consumed = True
        self._high_water = mid
        intent.message_id = mid
        intent.outcome = {
            "ok": True, "message_id": mid, "out_of_band": out_of_band,
        }
        self._signal_resolution(intent.request_id)

    async def process_intents_once(self) -> None:
        """One late/timeout intent pass (§2(4) late-arm, §2(5) timeout).

        Deterministic seam driven directly by unit tests; the background
        :meth:`run_watcher` calls it on a tick. Under the lock: a
        ``slot_missed`` armed intent posts out-of-band THREADED (no warn, no
        debt — its block already passed); an armed intent unmatched for
        ``intent_timeout_s`` posts out-of-band with a WARN and leaves a
        one-block consumption debt so its late frame is consumed silently.
        """
        async with self._serialized():
            self._arm_event.clear()
            now = self._now()
            for intent in self.registry.armed_unposted():
                if intent.slot_missed:
                    await self._post_intent_locked(intent, out_of_band=True)
                elif now - intent.registered_at >= self._intent_timeout_s:
                    await self._post_intent_locked(
                        intent, out_of_band=True, warn=True,
                    )
                    # §2(5): leave the one-block consumption debt. The intent is
                    # ``posted`` but stays matchable via ``timeout_posted`` so a
                    # subsequent same-hash intent can never bind its late block.
                    intent.consumed = False
                    intent.timeout_posted = True

    async def run_watcher(self) -> None:  # pragma: no cover - background loop
        """Background task: drive late/timeout discrete posts (§2). Wakes on an
        arm/cancel signal or a periodic tick (for the 10s timeout)."""
        while True:
            try:
                await asyncio.wait_for(
                    self._arm_event.wait(), timeout=self._hold_poll_s * 4,
                )
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            try:
                await self.process_intents_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "output sequencer watcher error (engagement %s): %s",
                    self.engagement_id, exc,
                )


async def _default_sleep(seconds: float) -> None:  # pragma: no cover - trivial
    await asyncio.sleep(seconds)
