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

# Canonical channel-MCP tool names (the ask/reply ingresses — §2 pinned
# ingress (a)). ``HOLD_ELIGIBLE_TOOLS`` is the set of tool kinds that ALWAYS
# correspond to a discrete post, so a block for one holds a slot even when no
# intent is registered yet. T2/T3 extend this set for emit_completion; permission
# keyboards fence on the GATED tool's own frame and are recognized reactively
# (only when a matching intent/tombstone already exists), never held blindly.
ASK_TOOL = "mcp__casa-engagement-channel__ask"
REPLY_TOOL = "mcp__casa-engagement-channel__reply"
HOLD_ELIGIBLE_TOOLS: frozenset[str] = frozenset({ASK_TOOL, REPLY_TOOL})


# ---------------------------------------------------------------------------
# Pinned projection → hash (§2, "Hash identity"): computed AT THE INGRESS
# BOUNDARY from RAW args under pinned projections, using the v0.76 canonical
# helper. The SAME function is applied by the relay to each tool_use block, so
# an intent's transmitted hash and the block's computed hash agree.
# ---------------------------------------------------------------------------


def project_args(tool_name: str, raw_args: dict) -> dict:
    """Apply the pinned projection for *tool_name* to *raw_args*.

    * ``ask`` → ``{question, options, timeout_s-as-given}``.
    * ``reply`` → ``{text}`` (drops the SDK-compat ``chat_id``).
    * everything else (a permission-gated tool's own frame, ``emit_completion``)
      → identity over the raw args.
    """
    if not isinstance(raw_args, dict):
        raw_args = {}
    if tool_name == ASK_TOOL:
        return {
            "question": raw_args.get("question"),
            "options": raw_args.get("options"),
            "timeout_s": raw_args.get("timeout_s"),
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

    # -- matchability predicates -------------------------------------------
    def matchable(self) -> bool:
        """Still eligible to bind a content-block (§2(3)).

        Pending/armed intents, un-consumed cancelled tombstones, and the
        one-block timeout-posted debt are matchable; a consumed item is not.
        """
        if self.consumed:
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
            if i.state == "armed" and not i.consumed and i.message_id is None
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
        self._now = _now
        self._sleep = _sleep or _default_sleep
        self._slot_hold_s = slot_hold_s
        self._intent_timeout_s = intent_timeout_s
        self._hold_poll_s = hold_poll_s

        self._lock = asyncio.Lock()
        self.registry = IntentRegistry(_now=_now)
        # HIGH-WATER: newest sequencer-posted message id (the sequencer is the
        # only writer, so it is authoritative). ``_narration_msg_id`` is the
        # current OPEN narration message, or None when narration is SEALED.
        self._high_water: int | None = None
        self._narration_msg_id: int | None = None
        # F1 no-op edit gate: msg_id -> (text, markup_tristate).
        self._edit_cache: dict[int, tuple[str, Any]] = {}
        self._arm_event = asyncio.Event()
        # v0.79.0 (§3, Primitive B): reply-threading. An inbound operator
        # envelope's delivery sets this to its Telegram message id; the turn's
        # FIRST sequencer-posted message (narration open, or an ask/reply
        # poster via ``consume_turn_reply_to``) threads to it, then clears it.
        self._turn_reply_to: int | None = None

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
        async with self._lock:
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
        async with self._lock:
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

    async def seal_narration(self) -> None:
        """Explicitly SEAL the open narration (nothing edits it again)."""
        async with self._lock:
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
        async with self._lock:
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

    def prune_turn(self) -> None:
        """§2(6): prune intents/tombstones/outcomes at turn end."""
        self.registry.prune()

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
            async with self._lock:
                resolved = await self._resolve_block_locked(tool_name, block_hash)
                if resolved is not None:
                    return resolved
            if self._now() >= deadline:
                async with self._lock:
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
                item.consumed = True  # §2(5) debt consumed
                return "debt_consumed"
            return None  # pending → HOLD
        if (
            tool_name not in HOLD_ELIGIBLE_TOOLS
            or not self.registry.has_any_matchable()
        ):
            # Not hold-eligible, OR hold-eligible but NO ingress is active
            # (registry empty ⇒ nothing can arrive to bind this block): proceed
            # immediately, never stall narration — the machinery stays dormant
            # in the T1-stubbed state.
            return "no_match"
        return None  # hold-eligible, ingress active, intent not here yet → HOLD

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
        intent.state = "posted"
        intent.consumed = True
        if mid is not None:
            self._high_water = mid
            intent.message_id = mid
        intent.outcome = {
            "ok": mid is not None, "message_id": mid, "out_of_band": out_of_band,
        }

    async def process_intents_once(self) -> None:
        """One late/timeout intent pass (§2(4) late-arm, §2(5) timeout).

        Deterministic seam driven directly by unit tests; the background
        :meth:`run_watcher` calls it on a tick. Under the lock: a
        ``slot_missed`` armed intent posts out-of-band THREADED (no warn, no
        debt — its block already passed); an armed intent unmatched for
        ``intent_timeout_s`` posts out-of-band with a WARN and leaves a
        one-block consumption debt so its late frame is consumed silently.
        """
        async with self._lock:
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
