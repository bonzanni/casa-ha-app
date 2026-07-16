"""claude_code driver — s6-rc-supervised claude CLI per engagement.

See docs/superpowers/specs/2026-04-23-3.5-plan4a-claude-code-driver-design.md.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from drivers import s6_rc
from drivers.brief import brief_task_for
from drivers.driver_protocol import DriverProtocol
from drivers.workspace import (
    engagement_log_dir, provision_workspace, render_log_run_script,
    render_run_script, write_casa_meta,
)
from engagement_registry import EngagementRecord
from settle_gate import confirmed_settle_edit

logger = logging.getLogger(__name__)

# v0.79.0 (§3 Primitive B) — durable inbound envelope spool.
# Priority lanes: ordinary FIFO (cap 10) + redirect FIFO (cap 3), total 13.
_ORDINARY_LANE_CAP = 10
_PRIORITY_LANE_CAP = 3
_SPOOL_FILENAME = ".inbound_spool.jsonl"

# Exact operator-facing copies (§3 "Exact copies"; wording is binding).
_REDIRECT_PREFIX = (
    "[OPERATOR REDIRECT — drop your current agenda, re-plan from this message]"
)
_RECEIPT_COPY = "📥 Received — I'll get to this after the current step."
# §4 boot reconciliation: appended below an open question's stored text when a
# restart orphans it. This is the RESTART-orphaned copy — distinct from the
# live-ask expiry settle (``channel_handlers._SETTLE_EXPIRED``, which becomes
# the operator-away "engagement paused" copy under F-EXPIRE): a boot-orphaned
# question has no live operator-away episode, so the plain "answer by text
# below" wording still applies.
_OPEN_Q_EXPIRED_SUFFIX = "\n⌛ expired — answer by text below"
# §4 free-text anchor: appended when the next operator message answers it.
_OPEN_Q_ANSWERED_SUFFIX = "\n✅ answered below"
# §A3(b) terminal finalize: a live anchor stranded by /cancel or /complete is
# settled (never left visually open forever) with an outcome-appropriate copy.
_OPEN_Q_CANCELLED_SUFFIX = "\n🛑 engagement ended — this question is closed"
# §A3(b) staged re-anchor markers (plain text, no keyboard): step-4 settles the
# OLD copy pointing down to the re-posted question; the step-3 persist-failure
# fallback settles the NEW copy pointing up to the still-live original.
_OPEN_Q_REPOSTED_BELOW = "↪ question re-posted below ↓"
_OPEN_Q_SEE_ABOVE = "↪ see the question above"
# §A3(b) retry-owner bounded backoff (Sol r13-1): a FAILED latch-consuming pass
# self-reschedules 5s → 30s → 300s (capped, repeated) indefinitely until the
# latch clears via success / promotion / terminal settlement / a boundary win.
_REANCHOR_BACKOFF: tuple[float, ...] = (5.0, 30.0, 300.0)
# F3 (whole-branch gate): after an UNVERIFIED (False/raised) force-suspend
# outcome the once-per-episode backstop re-arms, but must PACE before it may
# fire again — an immediate re-arm lets a doctrine-defying ask→refusal loop
# churn probes/subprocesses at token speed. A monotonic cooldown gates re-firing.
_AWAY_FORCE_COOLDOWN_S = 60.0
_EVICTION_COPY = (
    "⚠️ Dropped in favor of your redirect — resend if still relevant."
)
_PRIORITY_CAP_COPY = "⚠️ Too many pending redirects — this one was dropped."
_SPOOL_FAIL_COPY = "your message could not be recorded — please resend"
# An ordinary message arriving with the ordinary lane full keeps the existing
# dropped-full notice (§3: "an ordinary message at cap keeps the existing
# dropped-full notice").
_ORDINARY_FULL_COPY = (
    f"Your engagement already has {_ORDINARY_LANE_CAP} messages waiting to be "
    "delivered — this one was dropped. Please wait for it to catch up, then "
    "resend."
)


async def _never_deliver() -> bool:
    """A ``write_fifo`` for a drain-only spool view (reconcile): never delivers.
    """
    return False


async def _completion_noop_poster() -> int | None:
    """Poster for the emit_completion CONSUMPTION-DEBT intent (§2 F1(c)): never
    invoked (a debt block is consumed silently, never posted)."""
    return None


def _is_redirect(text: str) -> bool:
    """§3 redirect detection: ``STOP`` as the (case-insensitive) first line, or
    a ``redirect:`` prefix."""
    if not text:
        return False
    stripped = text.strip()
    if stripped.lower().startswith("redirect:"):
        return True
    first_line = stripped.splitlines()[0].strip() if stripped else ""
    return first_line.lower() == "stop"
# P31 (v0.37.10): match a UUID as a complete filename stem — the
# claude CLI names its session-storage files ``<uuid>.jsonl``. Replaces
# v0.37.9's free-text session_id regex (which tailed a log file that
# never gets created in production; see bug-review-2026-05-14-exploration6).
_UUID_REGEX = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}'
    r'-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

TopicSender = Callable[[int, str], Awaitable[Any]]
SessionIdPersister = Callable[[str, str], Awaitable[None]]
"""(engagement_id, session_id) → None — registry persist hook.

Matches engagement_registry.persist_session_id's bound-method signature."""


# Notice sender: (text, reply_to_message_id) -> delivered_ok.
NoticeSender = Callable[[str, "int | None"], Awaitable[bool]]

# Persisted envelope fields (§3 schema) + the impl-only fields the spool needs.
_ENVELOPE_PERSIST_FIELDS = (
    "text", "tg_message_id", "priority", "receipt", "notice", "notice_text",
    "enqueued_at", "delivery_epoch", "state", "seq", "is_initial",
    "answer_anchor_mid",
)


@dataclass
class _Envelope:
    """One durable inbound operator message (§3).

    Persisted schema: ``{text, tg_message_id, priority, receipt, notice,
    enqueued_at, delivery_epoch, state}``. ``seq`` (FIFO order within a lane)
    and ``is_initial`` (suppress interaction-state advance for the initial
    prompt) are impl fields carried on the same line.

    * ``receipt`` ∈ ``not_required | pending | sent`` — at-least-once receipt.
    * ``notice`` ∈ ``none | pending | sent`` — a pending eviction notice.
    * ``state`` ∈ ``queued | delivered | consumed`` — ``consumed`` only on
      turn_start evidence; ``delivered`` but died pre-turn_start ⇒ redelivered.
    """

    text: str
    tg_message_id: int | None
    priority: bool
    receipt: str
    notice: str
    enqueued_at: float
    delivery_epoch: int | None
    state: str
    seq: int
    is_initial: bool = False
    # §3 / F8: the notice copy to send when ``notice == "pending"``. ``None``
    # means the eviction copy (backward-compatible default); capacity-DROP
    # notices (priority-full / ordinary-full) carry their own copy on a
    # notice-only envelope so the drop notice is durable + retried, not
    # fire-and-forget.
    notice_text: str | None = None
    # v0.83.0 (§A3, Sol r7-1): the tg_message_id of the free-text anchor this
    # message ANSWERED, recorded at durable-enqueue promotion. When set,
    # delivery only THREADS the turn's first post to it (the promotion already
    # ran the visual settle) — delivery never re-settles. ``None`` (field
    # absent on legacy spooled envelopes) keeps the delivery-time settle path.
    answer_anchor_mid: int | None = None

    def to_line(self) -> str:
        return json.dumps(
            {k: getattr(self, k) for k in _ENVELOPE_PERSIST_FIELDS},
            ensure_ascii=False,
        )

    @classmethod
    def from_line(cls, line: str) -> "_Envelope | None":
        try:
            data = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or "text" not in data:
            return None
        return cls(
            text=data.get("text", ""),
            tg_message_id=data.get("tg_message_id"),
            priority=bool(data.get("priority", False)),
            receipt=data.get("receipt", "not_required"),
            notice=data.get("notice", "none"),
            notice_text=data.get("notice_text"),
            enqueued_at=float(data.get("enqueued_at", 0.0)),
            delivery_epoch=data.get("delivery_epoch"),
            state=data.get("state", "queued"),
            seq=int(data.get("seq", 0)),
            is_initial=bool(data.get("is_initial", False)),
            answer_anchor_mid=data.get("answer_anchor_mid"),
        )


class _InboundSpool:
    """Durable JSON-lines inbound envelope spool per engagement (§3).

    Replaces the ephemeral ``_InboundQueue``/``.inbound_pending`` marker. Every
    operator message is a durable envelope; delivery is REDELIVERY-BY-
    CONSTRUCTION (a message clears to ``consumed`` only on positive turn_start
    evidence, so a process death pre-turn_start redelivers it on the next
    spawn). Receipts and eviction notices are at-least-once with a durable
    tri-state, retried at every spool touchpoint (enqueue, turn start, turn
    end, boot recovery, terminal drain).

    Priority lanes: redirects (``STOP``/``redirect:``) drain ahead of the
    ordinary FIFO. Caps 10 (ordinary) / 3 (priority); a redirect at ordinary
    cap evicts the newest ordinary (threaded notice), a redirect at priority
    cap drops, an ordinary at cap drops.

    Injected primitives keep it unit-testable in isolation:
    ``write_fifo(text) -> ok``, ``send_notice(text, reply_to) -> ok`` (a
    delivered-ok bool so a failed send stays pending for retry),
    ``is_turn_running() -> bool`` (receipt is due iff a turn is running),
    ``current_epoch() -> int|None`` (stamps delivery / correlates consumption),
    and an optional ``sequencer`` (delivery sets the turn's reply-thread target)
    plus ``registry`` (interaction-state advance).
    """

    def __init__(
        self,
        *,
        engagement_id: str,
        spool_path: str,
        write_fifo: Callable[[str], Awaitable[bool]],
        send_notice: NoticeSender,
        is_turn_running: Callable[[], bool] = lambda: False,
        current_epoch: Callable[[], int | None] = lambda: None,
        sequencer: Any = None,
        registry: Any = None,
        supersede_pending_asks: Callable[[], Awaitable[None]] | None = None,
        settle_anchor_on_delivery: (
            Callable[[int | None], Awaitable[int | None]] | None) = None,
        on_operator_enqueued: Callable[[], Awaitable[None]] | None = None,
        promote_answer_on_enqueue: (
            Callable[[], Awaitable[int | None]] | None) = None,
    ) -> None:
        self._engagement_id = engagement_id
        self._spool_path = spool_path
        self._write_fifo = write_fifo
        self._send_notice = send_notice
        self._is_turn_running = is_turn_running
        self._current_epoch = current_epoch
        self._sequencer = sequencer
        self._registry = registry
        # v0.79.0 (§4): fired when a non-initial operator envelope is enqueued
        # while an ask keyboard is still PENDING — casa-main cancels it as
        # ``superseded_by_text`` so the operator's message doesn't dead-wait.
        self._supersede_pending_asks = supersede_pending_asks
        # F-EXPIRE (A2a): on a durable non-initial operator enqueue (a real
        # inbound envelope — NOT a dropped/capacity notice), end any operator-away
        # suspend episode. Fires from the SAME successful-persist path that bumps
        # the generation, so it is exactly "a durably-enqueued operator message
        # exists". The driver wires this to clear ``_operator_away`` + recompute.
        self._on_operator_enqueued = on_operator_enqueued
        # §4: on delivery of an operator message, settle the oldest open
        # free-text anchor (✅ answered below) and thread the turn to it.
        # Returns the anchor's tg_message_id to thread to, or None.
        self._settle_anchor_on_delivery = settle_anchor_on_delivery
        # §A3 (Sol r7-1/r9-1): at durable non-initial enqueue, PROMOTE the
        # answer — mark the oldest unanswered anchor answered, run the visual
        # settle, CONSUME any reservation. UNCONDITIONAL (any delivered
        # operator message answers the one open question). Returns the anchor's
        # tg_message_id (recorded on the envelope so delivery only THREADS),
        # or None when no anchor is open.
        self._promote_answer_on_enqueue = promote_answer_on_enqueue
        self._envelopes: list[_Envelope] = []
        self.reader_ready = False
        self._pump_lock = asyncio.Lock()
        self._next_seq = 0
        # v0.79.0 (§4): monotonic operator-message generation — the ask inbound
        # gate reserves this before BROKER.register and re-checks it after
        # posting to close the arrival race.
        self._generation = 0
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = Path(self._spool_path).read_text(encoding="utf-8")
        except OSError:
            return
        for line in raw.splitlines():
            if not line.strip():
                continue
            env = _Envelope.from_line(line)
            if env is not None:
                self._envelopes.append(env)
        self._next_seq = max((e.seq for e in self._envelopes), default=-1) + 1

    def _persist(self) -> None:
        """Atomic whole-file rewrite (temp + os.replace). Raises OSError on
        failure so the enqueue path can surface the spool-write-FAILURE notice.
        """
        payload = "".join(e.to_line() + "\n" for e in self._envelopes)
        tmp = f"{self._spool_path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._spool_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- lane / prune helpers ---------------------------------------------

    def _lane_members(self) -> list[_Envelope]:
        """Envelopes still eligible for delivery (queued, not evicted)."""
        return [
            e for e in self._envelopes
            if e.state == "queued" and e.notice == "none"
        ]

    def _ordinary_count(self) -> int:
        return sum(1 for e in self._lane_members() if not e.priority)

    def _priority_count(self) -> int:
        return sum(1 for e in self._lane_members() if e.priority)

    def _next_deliverable(self) -> "_Envelope | None":
        members = self._lane_members()
        if not members:
            return None
        priority = [e for e in members if e.priority]
        pool = priority if priority else members
        return min(pool, key=lambda e: e.seq)

    @staticmethod
    def _should_retain(env: _Envelope) -> bool:
        if env.notice == "pending":
            return True            # eviction notice still owed
        if env.receipt == "pending":
            return True            # receipt still owed (retried at touchpoints)
        # A live delivery candidate (queued/delivered and NOT an evicted
        # envelope whose notice already went out).
        if env.state in ("queued", "delivered") and env.notice != "sent":
            return True
        return False

    def _prune(self) -> None:
        self._envelopes = [e for e in self._envelopes if self._should_retain(e)]

    def has_pending(self) -> bool:
        return any(
            e.receipt == "pending" or e.notice == "pending"
            for e in self._envelopes
        )

    # -- v0.79.0 (§4) inbound-gate reads -----------------------------------

    def generation(self) -> int:
        """Monotonic operator-message generation (bumped on each successful
        enqueue). The ask gate reserves this, posts, then re-checks it — a
        change means an operator message arrived in the race window."""
        return self._generation

    def unread_depth(self) -> int:
        """Count of queued operator envelopes the agent has NOT yet seen
        (deliverable, not-yet-delivered, non-initial). ``depth > 0`` at ask
        entry means the operator is waiting — the ask is refused."""
        return sum(1 for e in self._lane_members() if not e.is_initial)

    # -- enqueue -----------------------------------------------------------

    async def enqueue(
        self, text: str, *, tg_message_id: int | None = None,
        is_initial: bool = False,
    ) -> str:
        """Append an operator envelope; return a durable disposition string
        (§3): ``queued`` | ``dropped_full`` | ``evicted_other(<tg_id>)`` |
        ``error`` — reported AFTER the atomic spool write.
        """
        is_redirect = (not is_initial) and _is_redirect(text)
        evicted_tg: int | None = None
        evicted = False
        if is_redirect:
            if self._priority_count() >= _PRIORITY_LANE_CAP:
                await self._record_drop_notice(_PRIORITY_CAP_COPY, tg_message_id)
                return "dropped_full"
            if self._ordinary_count() >= _ORDINARY_LANE_CAP:
                victim = max(
                    (e for e in self._lane_members() if not e.priority),
                    key=lambda e: e.seq, default=None,
                )
                if victim is not None:
                    victim.notice = "pending"       # retained for its notice
                    evicted_tg = victim.tg_message_id
                    evicted = True
        elif self._ordinary_count() >= _ORDINARY_LANE_CAP:
            await self._record_drop_notice(_ORDINARY_FULL_COPY, tg_message_id)
            return "dropped_full"

        receipt = (
            "pending" if (not is_initial and self._is_turn_running())
            else "not_required"
        )
        env = _Envelope(
            text=text, tg_message_id=tg_message_id, priority=is_redirect,
            receipt=receipt, notice="none", enqueued_at=time.time(),
            delivery_epoch=None, state="queued", seq=self._next_seq,
            is_initial=is_initial,
        )
        self._next_seq += 1
        self._envelopes.append(env)
        try:
            self._persist()
        except OSError as exc:
            # Spool-write FAILURE — the ONLY enqueue-time notice (§3, S4 fix).
            logger.warning(
                "engagement %s: inbound spool write failed: %s",
                self._engagement_id[:8], exc,
            )
            self._envelopes.pop()               # roll the in-memory add back
            if evicted:                         # un-evict the victim we touched
                for e in self._envelopes:
                    if e.tg_message_id == evicted_tg and e.notice == "pending":
                        e.notice = "none"
                        break
            await self._send_notice(_SPOOL_FAIL_COPY, tg_message_id)
            return "error"

        # §4: the enqueue succeeded — bump the operator-message generation and,
        # for a real operator turn (not the initial task), supersede any ask
        # keyboard still waiting for a tap (it must not dead-wait behind a
        # message the operator has already sent).
        self._generation += 1
        if not is_initial and self._supersede_pending_asks is not None:
            try:
                await self._supersede_pending_asks()
            except Exception:  # noqa: BLE001 — supersession is best-effort
                logger.debug("supersede_pending_asks failed", exc_info=True)
        # F-EXPIRE (A2a) EXIT: a durably-enqueued operator message ends the
        # operator-away episode (clear the flag + recompute the summary so the
        # ⏸ coercion window closes crisply). Best-effort; the initial task never
        # clears (there is no away state at spawn).
        if not is_initial and self._on_operator_enqueued is not None:
            try:
                await self._on_operator_enqueued()
            except Exception:  # noqa: BLE001 — away-clear is best-effort
                logger.debug("on_operator_enqueued failed", exc_info=True)

        # §A3 (Sol r7-1/r9-1): PROMOTE the answer at durable enqueue — the
        # message is now durably spooled, so the agent WILL receive it and the
        # question is answered. Promotion marks the oldest unanswered anchor
        # answered + runs its visual settle + CONSUMES any reservation, and
        # records the anchor mid on THIS envelope so delivery only threads
        # (never re-settles). Runs BEFORE ``_pump`` so an immediate delivery
        # sees the recorded mid. Best-effort: a failure only degrades the
        # delivery reply-thread (pinned advisory).
        if not is_initial and self._promote_answer_on_enqueue is not None:
            try:
                anchor_mid = await self._promote_answer_on_enqueue()
                if anchor_mid is not None:
                    env.answer_anchor_mid = anchor_mid
                    self._persist_quiet()
            except Exception:  # noqa: BLE001 — promotion settle is advisory
                logger.debug("promote_answer_on_enqueue failed", exc_info=True)

        await self._flush_pending()
        await self._pump()
        if evicted:
            return f"evicted_other({evicted_tg})"
        return "queued"

    # -- receipt / notice at-least-once flush ------------------------------

    async def _record_drop_notice(
        self, copy: str, tg_message_id: int | None,
    ) -> None:
        """F8: record a DURABLE capacity-drop notice (priority-full / ordinary-
        full). The dropped operator envelope itself is gone, but the notice must
        NOT be fire-and-forget — a failed send has to survive and retry. A
        notice-only envelope (``state=consumed``, ``notice=pending`` carrying its
        own ``notice_text``) is appended + persisted; it rides the same
        at-least-once retry lane as eviction notices (flushed here now, and again
        at every touchpoint until it sends). ``has_pending()`` stays True while
        the send has not succeeded."""
        env = _Envelope(
            text="", tg_message_id=tg_message_id, priority=False,
            receipt="not_required", notice="pending", notice_text=copy,
            enqueued_at=time.time(), delivery_epoch=None, state="consumed",
            seq=self._next_seq, is_initial=False,
        )
        self._next_seq += 1
        self._envelopes.append(env)
        self._persist_quiet()
        await self._flush_pending()

    async def _flush_pending(self) -> None:
        """Retry every pending receipt/notice (§3 touchpoint). Notice-first
        suppression: an envelope with BOTH pending sends ONLY the notice, then
        flips its receipt to ``not_required`` (one operator message, never two).
        """
        changed = False
        for env in self._envelopes:
            if env.notice == "pending":
                ok = await self._send_notice(
                    env.notice_text or _EVICTION_COPY, env.tg_message_id)
                if ok:
                    env.notice = "sent"
                    if env.receipt == "pending":
                        env.receipt = "not_required"   # notice-first suppression
                    changed = True
            elif env.receipt == "pending":
                ok = await self._send_notice(_RECEIPT_COPY, env.tg_message_id)
                if ok:
                    env.receipt = "sent"
                    changed = True
        if changed:
            self._prune()
            try:
                self._persist()
            except OSError as exc:                 # best-effort; retried next tick
                logger.warning(
                    "engagement %s: spool persist after flush failed: %s",
                    self._engagement_id[:8], exc,
                )

    def mark_all_settled(self) -> None:
        """Boot-reconciliation WARN-drop (§3): topic is gone — settle every
        pending receipt/notice so it stops retrying, WITHOUT sending."""
        for env in self._envelopes:
            if env.notice == "pending":
                env.notice = "sent"
            if env.receipt == "pending":
                env.receipt = "not_required"
        self._prune()

    # -- lifecycle touchpoints --------------------------------------------

    async def on_spawn(self) -> None:
        """A ``spawn`` control frame — arm the reader, REDELIVER any envelope
        still ``delivered`` (a prior epoch that never reached turn_start), pump.
        """
        self.reader_ready = True
        reverted = False
        for env in self._envelopes:
            if env.state == "delivered":
                env.state = "queued"
                env.delivery_epoch = None
                reverted = True
        if reverted:
            self._persist_quiet()
        await self._pump()

    async def on_turn_start(self) -> None:
        """turn_start evidence: the envelope delivered THIS epoch is now
        ``consumed``. Retry pending receipts/notices."""
        epoch = self._current_epoch()
        changed = False
        for env in self._envelopes:
            if env.state == "delivered" and env.delivery_epoch == epoch:
                env.state = "consumed"
                changed = True
        if changed:
            self._persist_quiet()
        await self._flush_pending()

    async def on_turn_end(self) -> None:
        """result / turn boundary: retry pending receipts/notices, prune."""
        await self._flush_pending()

    async def recover(self) -> None:
        """Boot recovery (replaces the zero-with-uncertainty notice path):
        revert stale ``delivered`` envelopes to ``queued`` (redelivery), retry
        pending receipts/notices, pump if already armed."""
        reverted = False
        for env in self._envelopes:
            if env.state == "delivered":
                env.state = "queued"
                env.delivery_epoch = None
                reverted = True
        if reverted:
            self._persist_quiet()
        await self._flush_pending()
        await self._pump()

    async def drain(self) -> None:
        """Terminal pre-close drain (§3): flush pending receipts/notices while
        the topic is still open."""
        await self._flush_pending()

    def _persist_quiet(self) -> None:
        try:
            self._persist()
        except OSError as exc:
            logger.warning(
                "engagement %s: spool persist failed: %s",
                self._engagement_id[:8], exc,
            )

    async def _pump(self) -> None:
        async with self._pump_lock:
            while self.reader_ready:
                env = self._next_deliverable()
                if env is None:
                    break
                text = env.text
                if env.priority:
                    text = f"{_REDIRECT_PREFIX}\n{env.text}"
                ok = await self._write_fifo(text)
                if not ok:
                    # Retain + stay armed — retry on the next spawn / enqueue.
                    break
                env.state = "delivered"
                env.delivery_epoch = self._current_epoch()
                self.reader_ready = False           # one message per FIFO EOF
                self._persist_quiet()
                # §4/§A3: thread this turn to the ANCHOR the message answered,
                # not the operator's own message. Promotion (at durable enqueue)
                # already ran the visual settle and recorded the anchor mid on
                # the envelope — so delivery only THREADS to it (no second
                # settle edit). A LEGACY envelope spooled before ``answer_anchor
                # _mid`` existed carries no mid → fall back to the delivery-time
                # settle path.
                thread_to = env.tg_message_id
                if not env.is_initial:
                    if env.answer_anchor_mid is not None:
                        thread_to = env.answer_anchor_mid
                    elif self._settle_anchor_on_delivery is not None:
                        try:
                            anchor_id = await self._settle_anchor_on_delivery(
                                env.tg_message_id)
                            if anchor_id is not None:
                                thread_to = anchor_id
                        except Exception:  # noqa: BLE001 — anchor settle advisory
                            logger.debug("settle_anchor_on_delivery failed",
                                         exc_info=True)
                # Delivery context: thread this turn's first sequencer post to
                # the operator's message (§3) — or the anchor it answered (§4).
                if self._sequencer is not None:
                    try:
                        self._sequencer.set_turn_reply_to(thread_to)
                    except Exception:  # noqa: BLE001 — advisory threading only
                        logger.debug("set_turn_reply_to failed", exc_info=True)
                # Advance interaction state for ordinary operator turns only.
                if not env.is_initial and self._registry is not None:
                    fn = getattr(
                        self._registry, "advance_interaction_state", None,
                    )
                    if fn is not None:
                        await fn(self._engagement_id, "operator_turn")


class ClaudeCodeDriver(DriverProtocol):
    """s6-rc orchestrator. Does not manage subprocesses directly."""

    def __init__(
        self,
        *,
        engagements_root: str,
        send_to_topic: TopicSender,
        casa_framework_mcp_url: str,
        persist_session_id: SessionIdPersister | None = None,
        edit_topic_message: Callable[[int, int, str], Awaitable[bool]] | None = None,
        delete_topic_message: Callable[[int, int], Awaitable[bool]] | None = None,
        send_topic_message_markup: Callable[..., Awaitable[int | None]] | None = None,
        edit_topic_message_markup: Callable[..., Awaitable[bool]] | None = None,
        pin_topic_message: Callable[[int, int], Awaitable[bool]] | None = None,
        registry: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        reanchor_retry_sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engagements_root = engagements_root
        # F3 (whole-branch gate): injectable monotonic clock for the force-suspend
        # re-arm cooldown (tests advance it deterministically; never patch a
        # shared module attribute).
        self._monotonic = monotonic
        # W-R1 (v0.81.0): injectable clock for the confirmed-edit settle retry
        # (``confirmed_settle_edit``). Kept injectable so tests stay fast and
        # NEVER patch ``<module>.asyncio.sleep`` (the shared module attribute).
        self._sleep = sleep
        # §A3(b) retry owner: a SEPARATE injectable clock for the re-anchor retry
        # backoff, so a test can record its schedule without polluting the
        # settle-gate's ``_sleep`` recorder. Defaults to ``sleep``.
        self._reanchor_retry_sleep = reanchor_retry_sleep or sleep
        # ``send_to_topic`` doubles as the relay's ``send_message`` primitive:
        # casa_core wires it to return the posted Telegram message_id (the relay
        # needs it to edit the rolling message), while notice/warning callers
        # ignore the return.
        self._send_to_topic = send_to_topic
        self._edit_topic_message = edit_topic_message
        self._delete_topic_message = delete_topic_message
        # A9 (v0.83.0): markup-capable topic send/edit primitives, injected into
        # the per-engagement OutputSequencer so post_discrete/edit_discrete drive
        # keyboard-bearing writes through the single writer.
        self._send_topic_message_markup = send_topic_message_markup
        self._edit_topic_message_markup = edit_topic_message_markup
        # v0.79.0 (§5): best-effort pin of the live summary message. Takes
        # ``(topic_id, message_id)`` and returns pin-ok. None (or an ungranted
        # can_pin_messages) leaves the summary unpinned but still live.
        self._pin_topic_message = pin_topic_message
        self._casa_framework_mcp_url = casa_framework_mcp_url
        self._persist_session_id = persist_session_id
        # ``advance_interaction_state`` (Task 7) lives on this registry; the
        # inbound queue reaches for it via getattr (no-op until it exists).
        self._registry = registry
        # Per-engagement background tasks (respawn poller, session-id capture,
        # ALWAYS-on live topic-stream relay, DEBUG log relay).
        self._tasks: dict[str, list[asyncio.Task]] = {}
        self._last_turn_ts: dict[str, float] = {}
        # v0.79.0 (§3): durable inbound envelope spool + per-turn reply-text set
        # (relay reply de-dup) + current spawn epoch pending a result
        # (abnormal-exit probe) + per-engagement turn-running flag (receipt is
        # due iff a turn is running — set at turn_start, cleared at result/spawn).
        self._inbound: dict[str, _InboundSpool] = {}
        self._reply_texts: dict[str, set[str]] = {}
        self._epoch_pending: dict[str, int | None] = {}
        self._turn_running: dict[str, bool] = {}
        # W2/Sol B9 (Task 7): at most ONE in-topic violation notice per
        # engagement — guards the mutating_tool seam in _on_stream_event.
        # B4 (Sol r1): the notice-post and the flag-persist are tracked
        # INDEPENDENTLY; each marker is set only AFTER its own effect succeeds,
        # so a transient failure of one retries on the next mutating_tool frame
        # instead of permanently skipping both.
        self._violation_notified: set[str] = set()
        self._violation_flagged: set[str] = set()
        # v0.79.0 (§2 Primitive A): ONE per-engagement OUTPUT SEQUENCER (the
        # single serialized topic writer that owns the high-water mark, the
        # no-op edit gate and the relay-mediated discrete-posting intent
        # registry). Shared between this engagement's topic-stream relay and the
        # discrete ingresses — the ask/reply/permission/emit_completion ingress
        # adapters (T2/T3) reach it through this driver's intent-registration
        # API (register_send_intent / arm_send_intent / cancel_send_intent),
        # resolving the driver via ``agent.active_claude_code_driver`` exactly
        # as tools.emit_completion already does.
        self._sequencers: dict[str, "OutputSequencer"] = {}
        # v0.79.0 (§5): ONE per-engagement live-SUMMARY controller (owns the
        # pinned first topic message; consumers submit desired state and it
        # coalesces/throttles edits through the sequencer). Dropped on teardown.
        self._summaries: dict[str, "SummaryController"] = {}
        # v0.79.0 (§4): consecutive ask-inbound-gate refusals in the CURRENT
        # turn (reset at turn_start). From the 3rd the refusal copy escalates +
        # a WARN counter is logged (soft anti-livelock — no hard force-end).
        self._ask_refusals: dict[str, int] = {}
        # v0.83.0 (§A3, Sol r6-4 + r7-2): in-memory ANSWERED overlay. When
        # ``mark_question_answered``'s STRICT persist raises (the durable envelope
        # is already spooled, so the agent WILL get the answer — the question must
        # not keep gating), the caller records the number here so the live process
        # treats the question answered immediately. It is UNIONED onto the
        # persisted flag by ``_effective_open_question_numbers`` (gates/summary/
        # re-anchor honor overlay ∪ persisted) and each later settle attempt
        # RETRIES the strict persist; crash convergence is owned by boot
        # reconciliation (the overlay is memory-only, dropped at teardown).
        self._answered_overlay: dict[str, set[int]] = {}
        # v0.83.0 (§A3, Sol r7-1/r8-1/r9-1): the answered-RESERVATION token map,
        # ``engagement_id -> (token, question_n)``. Set at Telegram handler entry
        # (same synchronous section as the high-water advance) for the oldest
        # unanswered anchor, carrying a unique per-message uuid token — so any
        # finalize that observed the answer's high-water also observes the
        # reservation. A reserved-but-unpromoted question counts as ANSWERED for
        # the effective/union view (gates/summary/re-anchor). ROLLBACK is
        # token-CAS'd (a later message's reservation is never clobbered);
        # PROMOTION at durable enqueue is UNCONDITIONAL and CONSUMES it.
        # Memory-only (crash convergence is boot reconciliation's job).
        self._answer_reservations: dict[str, tuple[str, int]] = {}
        # v0.83.0 (§A3(a)+(c), Sol r2-8/r3-6/r4-4/5): the per-engagement
        # ASK-MAINTENANCE lock + the ingress-reservation ``ask_inflight`` marker.
        #
        # PINNED LOCK DISCIPLINE: ``ask_maintenance_lock`` is taken ONLY on
        # driver/handler tasks with the sequencer lock NOT held — no
        # sequencer→maintenance edge exists. The relay-invoked poster runs UNDER
        # the sequencer lock and therefore NEVER acquires it (it clears the marker
        # lock-free instead — asyncio run-to-completion makes the synchronous
        # marker→durable-ownership handoff gap-free). The lock serializes the ask
        # ingress reservation (check pending predicate + set marker) against the
        # reply gate's check, so two concurrent asks can never both pass their
        # gates before one reaches durable ownership.
        self._ask_maint_locks: dict[str, asyncio.Lock] = {}
        # ``engagement_id -> request_id`` of an ask that passed its gates but has
        # not yet reached durable ownership (broker register for buttons /
        # add_open_question for anchors). Set under the maintenance lock at
        # ingress; cleared SYNCHRONOUSLY (no lock) at durable ownership and on
        # every terminal failure path. Memory-only, dropped at teardown.
        self._ask_inflight: dict[str, str] = {}
        # v0.83.0 (§A3(b), Sol r10-1/r11-1/r13-1): the per-engagement re-anchor-due
        # LATCH — the standing OBLIGATION to keep the oldest unanswered anchor LAST.
        # SET when a re-anchor pass fails/aborts or is suppressed by a reservation
        # that then rolls back; CONSUMED (checked + cleared, only on a True pass)
        # at EVERY turn boundary (result-after-on_turn_end, spawn-without-result,
        # terminal finalize, and the rollback/non-delivery completion itself).
        self._reanchor_due: set[str] = set()
        # ONE retry task per engagement (Sol r13-1 note 1): a FAILED latch-consuming
        # pass is its own retry owner — it self-reschedules with bounded backoff
        # (injectable) until the latch clears (success / promotion / terminal
        # settlement) or a boundary consumer beats it. Double-arm is a no-op;
        # CancelledError terminates WITHOUT rescheduling; cancelled at teardown.
        self._reanchor_retry_tasks: dict[str, asyncio.Task] = {}
        # §A3(b) boot reconciliation owner (Sol r6-3): readiness-gated reconcile
        # tasks for refused/terminal-with-questions records (attached records
        # retain theirs in ``self._tasks``). Retained here so they are not GC'd
        # before the Telegram-readiness barrier lifts; each self-removes on done.
        self._boot_reconcile_tasks: list[asyncio.Task] = []
        # F-EXPIRE (v0.83.0, A2a): operator-away suspend state. ``_operator_away``
        # is SET on ask expiry (generation-CAS, ``note_operator_away``) and
        # CLEARED on the next durable inbound operator envelope; while set, every
        # further ask is refused and the summary coerces to ⏸ paused.
        # ``_away_refusals`` counts consecutive away-refusals in the current
        # away-episode (Task 5's force-turn-boundary backstop reads it; reset when
        # away clears). In-memory only — a Casa restart lands in the same
        # FIFO-blocked suspended state anyway, so nothing is persisted.
        self._operator_away: dict[str, bool] = {}
        self._away_refusals: dict[str, int] = {}
        # F-EXPIRE (v0.83.0, A2b): the HARD backstop. On the 2nd away-refusal in
        # an episode the driver force-ends the CLI turn ONCE (``_away_suspend_fired``
        # gates re-firing; reset when away clears). Before signalling, the current
        # spawn epoch is stamped into ``_forced_suspend_epochs`` so the ensuing
        # respawn's abnormal-exit log reads "forced suspend (operator away)" at
        # INFO instead of the scary WARN. ``_force_turn_boundary`` is the injected
        # kill callable (defaults to the verified group-kill in s6_rc).
        self._forced_suspend_epochs: dict[str, int | None] = {}
        self._away_suspend_fired: set[str] = set()
        # F3 (whole-branch gate): monotonic deadline BEFORE which a re-armed
        # backstop may NOT re-fire (set after an unverified False/raised outcome;
        # cleared when the away episode ends). Paces probe/subprocess churn.
        self._away_force_cooldown_until: dict[str, float] = {}
        self._force_turn_boundary = s6_rc.force_turn_boundary
        # A2b (Sol A2 review): the in-flight force-suspend task, held per
        # engagement so ``_clear_operator_away`` (operator returned) can CANCEL a
        # backstop kill still verifying group extinction — the turn boundary the
        # operator's own message will now provide makes the forced one moot.
        self._force_tasks: dict[str, asyncio.Task] = {}
        # A2b (Sol A2 wave-3, Finding 2): handed-off post-SIGTERM cleanup tasks.
        # When a force-suspend task is cancelled after SIGTERM was delivered,
        # ``force_turn_boundary`` hands its shielded extinction-poll + SIGKILL
        # escalation to ``_register_force_cleanup``. Those tasks are bounded
        # (≤ ~7 s) and MUST run to completion — teardown NEVER cancels this set
        # (unlike ``_tasks``). Tracked in a DEDICATED set (not ``_tasks[eng_id]``,
        # which teardown pops+cancels and which a post-teardown handoff would
        # otherwise resurrect as a stale entry); each task self-retires via an
        # ``add_done_callback`` so completion leaves no reference.
        self._force_cleanups: set[asyncio.Task] = set()

    # -- DriverProtocol ---------------------------------------------------

    async def start(
        self, engagement: EngagementRecord, prompt: str, options: Any,
    ) -> None:
        """options is the ExecutorDefinition — see DriverProtocol.start docstring.

        Bug 13 (v0.14.6): if any step from provision_workspace through
        start_service fails, roll back the partial state (workspace,
        service dir, s6-rc compile) so the engagement registry / sweeper
        don't end up with a permanent UNDERGOING ghost. The exception is
        re-raised so engage_executor's caller surfaces the failure.
        """
        import shutil
        defn = options
        # Workspace path is deterministic — compute it up front so the
        # rollback path can rmtree even if provision_workspace raises
        # before returning the assignment.
        ws_path = str(Path(self._engagements_root) / engagement.id)
        service_dir_written = False
        async with s6_rc._compile_lock:
            try:
                # M4: precompute executor_memory if the executor opts in.
                # Forward-compat — no claude_code executor opts in today, but
                # threading the slot now means a future memory-enabled
                # claude_code executor (e.g. claude_code-flavoured
                # configurator) works without further plumbing. Lazy import
                # of tools avoids a top-level cycle (drivers ← agent ← drivers);
                # _fetch_executor_archive lazily imports agent itself.
                executor_memory_block = ""
                if defn.memory.enabled:
                    from tools import _fetch_executor_archive
                    executor_memory_block = await _fetch_executor_archive(
                        task=engagement.task,
                        origin_channel=engagement.origin.get("channel", "telegram"),
                        token_budget=defn.memory.token_budget,
                    )

                # §3.3: a workspace-template/ (e.g. plugin-developer) selects
                # the template render path — independent of plugin assignment.
                exec_dir = Path(defn.prompt_template_path).parent
                template_root = exec_dir / "workspace-template"

                # 1. Provision workspace (CLAUDE.md, .mcp.json, FIFO, meta).
                ws = await provision_workspace(
                    engagements_root=self._engagements_root,
                    engagement_id=engagement.id,
                    defn=defn,
                    # W3 (Task 8): the CLAUDE.md {task} carries the full brief
                    # envelope (acceptance criteria + verbatim process
                    # requirements + completion accounting), derived from the
                    # RAW origin["brief"]; falls back to engagement.task when
                    # the engagement has no brief.
                    task=brief_task_for(engagement, defn),
                    context=engagement.origin.get("context", ""),
                    world_state_summary=engagement.origin.get("world_state_summary", ""),
                    casa_framework_mcp_url=self._casa_framework_mcp_url,
                    workspace_template_root=(
                        template_root if template_root.is_dir() else None
                    ),
                    executor_memory=executor_memory_block,
                )
                write_casa_meta(
                    workspace_path=ws,
                    engagement_id=engagement.id,
                    executor_type=defn.type,
                    status="UNDERGOING",
                    created_at=_iso_now(),
                    finished_at=None, retention_until=None,
                    # §3.8: record the pinned artifacts with the workspace meta.
                    plugin_artifacts=list(
                        getattr(engagement, "plugin_artifacts", ()) or ()),
                )

                # 2. Write the s6 service pair (sibling logger service
                #    captures the CLI's stdout — see s6_rc.write_service_dir).
                # v0.14.9: GITHUB_TOKEN is set at addon boot via
                # setup-configs.sh → /run/s6/container_environment/GITHUB_TOKEN, and
                # s6-overlay merges it into every supervised service's env. Engagement
                # subprocesses inherit it without per-engagement plumbing.
                extra_env: dict[str, str] = {}
                run_script = render_run_script(
                    engagement_id=engagement.id,
                    permission_mode=defn.permission_mode or "acceptEdits",
                    extra_dirs=list(defn.extra_dirs),
                    extra_env=extra_env or None,
                    # §3.8: load the pinned artifacts via --plugin-dir flags.
                    plugin_dirs=[pa["path"] for pa in
                                 getattr(engagement, "plugin_artifacts", ()) or ()],
                )
                log_script = render_log_run_script(engagement_id=engagement.id)
                s6_rc.write_service_dir(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=engagement.id,
                    run_script=run_script,
                    depends_on=["init-setup-configs"],
                    log_run_script=log_script,
                )
                service_dir_written = True

                # 3. Compile + update + change — lock held, inner helper.
                await s6_rc._compile_and_update_locked()
                # v0.79.0 (§5): post the pinned live SUMMARY and persist its id
                # BEFORE the subprocess starts. A post FAILURE aborts the launch
                # (rolled back by the handler below), so the operator never sees
                # an engagement running without its summary anchor.
                await self._post_initial_summary(engagement)
                await s6_rc.start_service(engagement_id=engagement.id)
            except Exception as start_exc:  # noqa: BLE001 — rollback is opportunistic
                logger.warning(
                    "claude_code start failed for engagement %s: %s; rolling back",
                    engagement.id[:8], start_exc,
                )
                # Best-effort rollback. Each step swallows its own errors so
                # one rollback failure doesn't mask the original cause.
                # v0.64.0: ALWAYS attempt dir removal — write_service_dir can
                # raise midway (pair half-written), and remove_service_dir is
                # idempotent. Recompile only when the dirs were fully written
                # (before that, the live db never saw them).
                try:
                    s6_rc.remove_service_dir(
                        svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                        engagement_id=engagement.id,
                    )
                except Exception as rb_exc:  # noqa: BLE001
                    logger.warning(
                        "rollback remove_service_dir failed: %s", rb_exc,
                    )
                if service_dir_written:
                    try:
                        await s6_rc._compile_and_update_locked()
                    except Exception as rb_exc:  # noqa: BLE001
                        logger.warning(
                            "rollback compile_and_update failed: %s", rb_exc,
                        )
                # Always attempt to remove the workspace tree at the
                # deterministic path — provision_workspace may have raised
                # AFTER creating partial state.
                try:
                    shutil.rmtree(ws_path, ignore_errors=True)
                except Exception as rb_exc:  # noqa: BLE001
                    logger.warning(
                        "rollback rmtree(%s) failed: %s", ws_path, rb_exc,
                    )
                raise

        # 4. Kick off the background tasks (outside lock): respawn poller,
        #    session-id capture, and (at DEBUG) the log relay.
        self._spawn_background_tasks(engagement)

        # 5. Enqueue the initial prompt (is_initial=True) — the first spawn
        #    arms the reader and delivers it. Enqueue is instant while the
        #    reader is unarmed, so no background task is needed.
        if prompt:
            spool = self._inbound.get(engagement.id)
            if spool is not None:
                await spool.enqueue(prompt, is_initial=True)
            else:
                # Background tasks disabled (e.g. a unit test) — legacy direct
                # write so start() still delivers the prompt.
                await self._write_to_fifo(engagement, prompt)

        logger.info("claude_code engagement %s started", engagement.id[:8])

    async def send_user_turn(
        self, engagement: EngagementRecord, text: str,
        *, tg_message_id: int | None = None,
    ) -> str | None:
        """Enqueue an operator turn; return the durable enqueue DISPOSITION
        (§A3, Sol r10-2) — ``queued`` / ``evicted_other(<tg>)`` (accepted, the
        promotion already ran) or ``dropped_full`` / ``error`` (rejected — the
        caller rolls back the answer reservation). ``None`` when there is no
        spool (legacy direct write)."""
        spool = self._inbound.get(engagement.id)
        if spool is not None:
            return await spool.enqueue(text, tg_message_id=tg_message_id)
        await self._write_to_fifo(engagement, text)
        return None

    async def cancel(self, engagement: EngagementRecord) -> None:
        """Teardown for a terminal transition (cancelled or completed).

        The durable spool FILE is intentionally left on disk: any pending
        receipts/notices are drained pre-close by ``_finalize_engagement`` and,
        if that drain crashed, the terminal boot-reconciliation owner picks
        them up. Only the in-memory spool object is dropped here."""
        # Cancel background tasks
        for t in self._tasks.pop(engagement.id, []):
            t.cancel()
        self._last_turn_ts.pop(engagement.id, None)
        self._inbound.pop(engagement.id, None)
        self._reply_texts.pop(engagement.id, None)
        self._epoch_pending.pop(engagement.id, None)
        self._turn_running.pop(engagement.id, None)
        self._violation_notified.discard(engagement.id)
        self._violation_flagged.discard(engagement.id)
        self._ask_refusals.pop(engagement.id, None)
        # v0.83.0 (§A3): drop the in-memory answered overlay + answer
        # reservation (memory-only; crash convergence is owned by boot
        # reconciliation, not these).
        self._answered_overlay.pop(engagement.id, None)
        self._answer_reservations.pop(engagement.id, None)
        # §A3(c): drop the ask-maintenance lock + ingress marker on teardown.
        self._ask_maint_locks.pop(engagement.id, None)
        self._ask_inflight.pop(engagement.id, None)
        # §A3(b): drop the re-anchor-due latch + cancel any retry-owner task
        # (CancelledError terminates it without rescheduling).
        self._reanchor_due.discard(engagement.id)
        retry_task = self._reanchor_retry_tasks.pop(engagement.id, None)
        if retry_task is not None and not retry_task.done():
            retry_task.cancel()
        # F-EXPIRE: drop the operator-away suspend state on teardown.
        self._operator_away.pop(engagement.id, None)
        self._away_refusals.pop(engagement.id, None)
        # A2b: drop the force-end backstop state on teardown.
        self._forced_suspend_epochs.pop(engagement.id, None)
        self._away_suspend_fired.discard(engagement.id)
        self._away_force_cooldown_until.pop(engagement.id, None)  # F3
        # Whole-branch gate r3: cancel IN PLACE — never pop-then-cancel. The
        # owner must stay VISIBLE in ``_force_tasks`` until its done callback
        # retires it, because its shielded post-SIGTERM cleanup only registers
        # in ``_force_cleanups`` when the cancellation actually runs; a
        # popped-but-not-yet-done owner would be invisible to
        # ``drain_force_cleanups``'s snapshot on BOTH surfaces.
        force_task = self._force_tasks.get(engagement.id)
        if force_task is not None and not force_task.done():
            force_task.cancel()
        # Sol A2 wave-3, Finding 2: ``_force_cleanups`` (handed-off post-SIGTERM
        # kill sequences) is DELIBERATELY NOT cancelled here — those bounded tasks
        # must complete their SIGKILL escalation + extinction verification. They
        # self-retire via their own done-callback; teardown just leaves them.
        # v0.79.0 (§2): drop the sequencer (its watcher task is cancelled above).
        self._sequencers.pop(engagement.id, None)
        # v0.79.0 (§5): drop the summary controller and cancel its elapsed tick
        # (not in self._tasks — the controller owns it).
        ctrl = self._summaries.pop(engagement.id, None)
        if ctrl is not None:
            ctrl.shutdown()

        async with s6_rc._compile_lock:
            # Stop is tolerant of "already down" — log and continue.
            try:
                await s6_rc.stop_service(engagement_id=engagement.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stop_service(%s) failed: %s",
                               engagement.id[:8], exc)
            # v0.64.0: also stop the sibling logger service explicitly so the
            # recompile below never has to down a still-live service. No-op
            # for legacy engagements without one.
            try:
                await s6_rc.stop_log_service(engagement_id=engagement.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stop_log_service(%s) failed: %s",
                               engagement.id[:8], exc)
            try:
                s6_rc.remove_service_dir(
                    svc_root=s6_rc.ENGAGEMENT_SOURCES_ROOT,
                    engagement_id=engagement.id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("remove_service_dir(%s) failed: %s",
                               engagement.id[:8], exc)
            try:
                await s6_rc._compile_and_update_locked()
            except Exception as exc:  # noqa: BLE001
                logger.warning("compile_and_update after remove failed: %s", exc)

    async def resume(self, engagement: EngagementRecord, session_id: str) -> None:
        """Effectively a no-op under s6 — the run script reads .session_id on
        its next spawn. Included for DriverProtocol completeness."""
        return

    def is_alive(self, engagement: EngagementRecord) -> bool:
        """Synchronous probe — schedules an async s6-svstat call and waits.

        Called from sweep code that is already async; use is_alive_async
        if you need awaitable form. This sync wrapper exists only to
        match DriverProtocol.is_alive signature.
        """
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Can't block the running loop; return True optimistically.
            # Callers in async context should use is_alive_async().
            return engagement.id in self._tasks
        return loop.run_until_complete(self.is_alive_async(engagement))

    async def is_alive_async(self, engagement: EngagementRecord) -> bool:
        pid = await s6_rc.service_pid(engagement_id=engagement.id)
        return pid is not None

    # -- internal ---------------------------------------------------------

    async def _write_to_fifo(
        self, engagement: EngagementRecord, text: str,
        *, timeout_s: float = 20.0, poll_s: float = 0.25,
    ) -> bool:
        """Write one newline-terminated line to the engagement FIFO.

        Returns ``True`` iff the WHOLE line was written (the inbound queue
        keys one-message-per-spawn delivery + retention on this). Any
        no-reader / stall / broken-pipe / missing-FIFO outcome returns
        ``False`` so the caller retains the item for the next spawn.
        """
        # M13: a blocking open(fifo, "a") parks a pooled executor thread
        # FOREVER when no reader exists (crash-looping/downed s6 service).
        # asyncio.to_thread threads are uncancellable, so a handful of stuck
        # writes starve all subprocess orchestration app-wide. Open + write
        # non-blocking with a bounded deadline instead — no thread at all.
        fifo = (Path(self._engagements_root) / engagement.id / "stdin.fifo")
        if not fifo.exists():
            logger.warning("FIFO missing for engagement %s", engagement.id[:8])
            return False
        data = (text + "\n").encode("utf-8")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        fd: int | None = None
        try:
            # O_NONBLOCK open raises ENXIO while no reader exists — retry until
            # a reader appears or the deadline passes (covers the ~1s s6
            # respawn pause without ever parking a thread).
            while fd is None:
                try:
                    fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
                except OSError as exc:
                    if exc.errno != errno.ENXIO:
                        logger.warning(
                            "FIFO open failed for engagement %s: %s",
                            engagement.id[:8], exc,
                        )
                        return False
                    if loop.time() >= deadline:
                        logger.warning(
                            "engagement %s: no FIFO reader after %.0fs — "
                            "dropping turn", engagement.id[:8], timeout_s,
                        )
                        # F3 (Sol r3): route the no-reader notice THROUGH the
                        # single writer (post_platform_notice: seals narration +
                        # advances high-water), not a direct send that bypasses
                        # the sequencer on an abnormal respawn. ``post_topic_notice``
                        # falls back to a direct send only when no live sequencer
                        # exists.
                        await self.post_topic_notice(
                            engagement,
                            "The engagement isn't accepting input right now — "
                            "your message was not delivered. Try again, or "
                            "/cancel if it stays unresponsive.",
                        )
                        return False
                    await asyncio.sleep(poll_s)
            # Reader exists; write non-blocking under the same deadline. Turns
            # are far below the 64KB pipe buffer, so the first write virtually
            # always completes fully.
            view = memoryview(data)
            while view:
                try:
                    n = os.write(fd, view)
                    view = view[n:]
                except BlockingIOError:
                    if loop.time() >= deadline:
                        logger.warning(
                            "engagement %s: FIFO write stalled — dropping "
                            "remainder of turn", engagement.id[:8],
                        )
                        return False
                    await asyncio.sleep(poll_s)
                except BrokenPipeError:
                    logger.warning(
                        "engagement %s: FIFO reader vanished mid-write",
                        engagement.id[:8],
                    )
                    return False
        finally:
            if fd is not None:
                os.close(fd)
        self._last_turn_ts[engagement.id] = time.time()
        return True

    def _spawn_background_tasks(
        self, engagement: EngagementRecord, *,
        reconcile_snapshot: list[dict] | None = None,
        reconcile_claimed: set[str] | None = None,
        telegram_ready: "asyncio.Event | None" = None,
    ) -> None:
        # Sol r2-B6: boot replay calls this DIRECTLY (not start), so the inbound
        # spool, reply-text set, epoch tracker AND the spool recovery all live
        # here — a resumed engagement gets the same wiring as a fresh one.
        ws = Path(self._engagements_root) / engagement.id
        self._reply_texts.setdefault(engagement.id, set())
        self._epoch_pending.setdefault(engagement.id, None)
        self._turn_running.setdefault(engagement.id, False)
        # v0.79.0 (§2): the shared per-engagement OUTPUT SEQUENCER + its late/
        # timeout discrete-post watcher task. Created BEFORE the relay so a
        # discrete ingress that registers an intent early always finds it, and
        # BEFORE the spool so delivery can set the turn's reply-thread target.
        sequencer = self._ensure_sequencer(engagement)
        # v0.79.0 (§5): the live-SUMMARY controller. Adopts the summary message
        # id posted at boot (fresh engagement) or persisted across a restart
        # (resumed engagement); its edits flow through the sequencer above.
        summary = self._ensure_summary(engagement)
        summary.adopt_message_id(
            getattr(engagement, "summary_message_id", None))
        # v0.79.0 (§3): the durable inbound envelope spool. Loads any surviving
        # spool file so undelivered turns redeliver and pending receipts/notices
        # retry (recovery scheduled below).
        self._inbound[engagement.id] = _InboundSpool(
            engagement_id=engagement.id,
            spool_path=str(ws / _SPOOL_FILENAME),
            write_fifo=lambda text: self._write_to_fifo(engagement, text),
            send_notice=lambda text, reply_to: self._spool_send_notice(
                engagement, text, reply_to),
            is_turn_running=lambda: self._turn_running.get(engagement.id, False),
            current_epoch=lambda: self._epoch_pending.get(engagement.id),
            sequencer=sequencer,
            registry=self._registry,
            supersede_pending_asks=lambda: self._supersede_pending_asks(
                engagement.id),
            settle_anchor_on_delivery=lambda op_mid: self._settle_open_anchor(
                engagement, op_mid),
            on_operator_enqueued=lambda: self._clear_operator_away(
                engagement.id),
            promote_answer_on_enqueue=lambda: self._promote_answer_on_enqueue(
                engagement),
        )

        tasks = [
            asyncio.create_task(self._poll_respawns(engagement)),
            asyncio.create_task(
                sequencer.run_watcher(),
                name=f"seq_watcher:{engagement.id[:8]}"),
            # P31 (v0.37.10): capture the SDK session_id by watching the
            # claude CLI's own session-storage directory. Persists the
            # UUID to ``<workspace>/.session_id`` so the run script's
            # ``--resume $(cat .session_id)`` plumbing picks up after a
            # Casa restart.
            asyncio.create_task(self._capture_session_id(engagement)),
            # W1: the LIVE topic-stream relay is spawned ALWAYS, regardless of
            # LOG_LEVEL — it is the operator's live window on the engagement,
            # not a debug aid. It fans ``on_turn_event`` into _on_stream_event
            # (arm the inbound queue on spawn, abnormal-exit correlation,
            # reply-text reset, Task-7 seams).
            asyncio.create_task(
                self._run_topic_relay(engagement),
                name=f"topic_relay:{engagement.id[:8]}"),
            # v0.79.0 (§5): initial best-effort pin attempt for the live summary
            # (retried on every lifecycle flush by the controller). No-op when
            # there is no pin primitive / no message id.
            asyncio.create_task(
                summary.ensure_pinned(),
                name=f"summary_pin:{engagement.id[:8]}"),
        ]
        # Phase 4b G5: ALSO relay every raw s6-log line into Casa's logger at
        # DEBUG so operators have one greppable namespace for both drivers' CLI
        # subprocess output. Spawned only when DEBUG-enabled: the tailer
        # re-opens and reads the file at 10 Hz, and at INFO every line would be
        # discarded. A LOG_LEVEL flip requires an add-on restart, which
        # respawns these tasks anyway. (Distinct from the always-on relay
        # above, which drives the operator-visible topic stream.)
        if logging.getLogger("subprocess_cli").isEnabledFor(logging.DEBUG):
            log_path = os.path.join(
                engagement_log_dir(engagement.id), "current")
            tasks.append(asyncio.create_task(
                self._relay_log_lines(engagement, log_path=log_path)))
        # v0.79.0 (§3): spool recovery replaces the zero-with-uncertainty
        # notice. A surviving spool redelivers undelivered turns by construction
        # and retries any pending receipts/notices — no "please resend" guess.
        # Scheduled only when a spool file survives (a fresh engagement has
        # nothing to recover), mirroring the old conditional marker reconcile.
        spool = self._inbound.get(engagement.id)
        if spool is not None and (ws / _SPOOL_FILENAME).exists():
            tasks.append(asyncio.create_task(spool.recover()))

        # v0.79.0 (§4): boot reconciliation of open questions. The broker /
        # finish hooks / reply anchors are memory-only, so a restart can leave a
        # question visibly open with a stale keyboard nobody can settle. Any
        # open_questions entry with no LIVE broker record settles here (expired
        # copy, keyboard cleared) while next_question_number is preserved.
        # Snapshot the open_questions SYNCHRONOUSLY here (attach time): every
        # entry on disk is by definition PRIOR-PROCESS (the in-memory broker
        # starts empty), so the reconcile settles this exact snapshot
        # unconditionally. Snapshotting at schedule time (not inside the task)
        # keeps a fresh same-process ask — which registers a NEW numbered entry
        # concurrently — out of the settle set (review I1: a live ask must not
        # suppress settling genuinely-stale prior-process keyboards).
        #
        # §A3(b) boot reconciliation owner (Sol r6-3/r7-3/4/r10-3): when casa_core
        # passes a PRE-SERVICE snapshot + shared claimed-set + the Telegram
        # readiness event, this attached record CLAIMS itself (exactly one
        # reconciler per record per boot) and its reconcile EXECUTION is gated on
        # channel readiness — so its confirmed settle edits never fire against a
        # ``None`` bot. Direct/legacy callers (driver unit tests, fresh start with
        # no snapshot) keep the immediate attach-time read + ungated schedule.
        # B3 — explicit snapshot contract: a replay context passes an EXPLICIT
        # per-record snapshot (possibly ``[]``). A snapshot that is not None means
        # replay ran — settle EXACTLY that pre-service snapshot (``[]`` ⇒ nothing
        # prior-process; a fresh same-process ask created between the snapshot and
        # this attachment is NOT in it and must NOT be expired). ``None`` means NO
        # replay context (legacy / direct callers, driver unit tests) → keep the
        # immediate attach-time fresh read + ungated schedule.
        if reconcile_snapshot is not None:
            rt = self.schedule_boot_reconcile(
                engagement, reconcile_snapshot, telegram_ready,
                claimed=reconcile_claimed)
            if rt is not None:
                tasks.append(rt)
        elif reconcile_claimed is not None or telegram_ready is not None:
            # Boot context but no per-record snapshot supplied (not produced by
            # casa_core post-B3; kept for a caller that gates without a snapshot):
            # fresh attach-time read, still claimed + readiness-gated.
            snap = list(getattr(engagement, "open_questions", ()) or ())
            rt = self.schedule_boot_reconcile(
                engagement, snap, telegram_ready, claimed=reconcile_claimed)
            if rt is not None:
                tasks.append(rt)
        else:
            attach_open_qs = list(
                getattr(engagement, "open_questions", ()) or ())
            if attach_open_qs:
                tasks.append(asyncio.create_task(
                    self.reconcile_open_questions(engagement, attach_open_qs)))

        self._tasks[engagement.id] = tasks

    def schedule_boot_reconcile(
        self, engagement: EngagementRecord, snapshot: list[dict],
        telegram_ready: "asyncio.Event | None", *,
        claimed: set[str] | None = None,
    ) -> "asyncio.Task | None":
        """§A3(b) boot reconciliation owner: schedule a readiness-gated reconcile
        of the record's PRE-SERVICE open-questions ``snapshot``. CLAIMS the record
        via the shared ``claimed`` set (exactly one reconciler per record per
        boot — a record already claimed returns ``None``). An empty snapshot
        needs no reconcile (returns ``None``). Retains the task so it isn't GC'd
        before the readiness barrier lifts; each self-removes on done. Used by
        BOTH the attached path (``_spawn_background_tasks``) and the casa_core
        refused/terminal-with-questions path."""
        eng_id = engagement.id
        if claimed is not None:
            if eng_id in claimed:
                return None
            claimed.add(eng_id)
        if not snapshot:
            return None
        task = asyncio.create_task(
            self._reconcile_after_ready(engagement, list(snapshot), telegram_ready),
            name=f"boot_reconcile:{eng_id[:8]}")
        self._boot_reconcile_tasks.append(task)
        task.add_done_callback(self._on_boot_reconcile_done)
        return task

    def _on_boot_reconcile_done(self, task: asyncio.Task) -> None:
        try:
            self._boot_reconcile_tasks.remove(task)
        except ValueError:
            pass

    async def _reconcile_after_ready(
        self, engagement: EngagementRecord, snapshot: list[dict],
        telegram_ready: "asyncio.Event | None",
    ) -> None:
        """§A3(b) channel-readiness barrier (Sol r9-2/r10-3): wait for the
        Telegram channel's first successful ``_rebuild`` before running the
        reconcile's confirmed settle edits — otherwise every edit fails closed
        against a ``None`` bot and the SAME ordering repeats next boot (settle
        would never happen). The snapshot was taken PRE-SERVICE; execution is
        deferred here, not in replay (which must not block on the channel)."""
        if telegram_ready is not None:
            await telegram_ready.wait()
        await self.reconcile_open_questions(engagement, snapshot)

    async def _spool_send_notice(
        self, engagement: EngagementRecord, text: str, reply_to: int | None,
    ) -> bool:
        """Send a spool receipt/notice into the topic, threaded to the operator
        message when given. Returns delivered-ok so a failed send stays pending
        for at-least-once retry (§3).

        v0.79.0 (§2 F1(b)): a receipt/notice is a DISCRETE platform-origin send
        and MUST go through the single writer — a receipt slipping in below open
        narration while ``edit_narration_if_latest`` still returns APPLIED is the
        exact ordering violation §2 forbids. It has no subprocess frame, so it
        registers no intent; instead the sequencer's ``post_platform_notice``
        seals open narration, posts, and advances the high-water mark under the
        one serialization lock. Falls back to a direct send only when no live
        sequencer exists (terminal boot reconciliation of a torn-down topic)."""
        try:
            seq = self._sequencers.get(engagement.id)
            if seq is not None:
                mid = await seq.post_platform_notice(text, reply_to=reply_to)
                return mid is not None
            if reply_to is not None:
                await self._send_to_topic(
                    engagement.topic_id, text, reply_to_message_id=reply_to)
            else:
                await self._send_to_topic(engagement.topic_id, text)
            return True
        except Exception as exc:  # noqa: BLE001 — retried at the next touchpoint
            logger.warning(
                "engagement %s: spool notice send failed (retried): %s",
                engagement.id[:8], exc,
            )
            return False

    async def post_topic_notice(
        self, engagement: EngagementRecord, text: str,
        reply_to: int | None = None,
    ) -> bool:
        """§2 F2: post a platform-origin notice (a Telegram command reply, a
        resume error) into the engagement topic THROUGH the single writer.

        Command replies used to post via a direct ``send_to_topic`` that
        bypassed the sequencer — a reply slipping in below open narration while
        ``edit_narration_if_latest`` still returns APPLIED is the exact ordering
        violation §2 forbids. Delegates to the same ``post_platform_notice``
        seam receipts use (seals narration + advances high-water under the one
        lock; direct send only when no live sequencer)."""
        return await self._spool_send_notice(engagement, text, reply_to)

    async def drain_inbound_spool(self, engagement: EngagementRecord) -> None:
        """§3 terminal pre-close drain: flush pending receipts/notices while
        the topic is still open (called by ``_finalize_engagement`` BEFORE the
        terminal commit + topic close)."""
        spool = self._inbound.get(engagement.id)
        if spool is not None:
            await spool.drain()

    async def finalize_completion_post(
        self, engagement: EngagementRecord, summary_text: str,
    ) -> bool:
        """§2 F1(c): post the engagement COMPLETION text through the single
        writer, but only AFTER draining the sequencer.

        Completion may not overtake its causal block: first flush every pending
        narration + parked/armed intent (``flush_armed_intents`` resolves late
        armed intents; sealing closes open narration), THEN post the completion
        text through ``post_platform_notice`` (single writer, seals + advances
        high-water). Returns ``True`` if a live sequencer posted it (the caller
        skips its own direct ``send_to_topic``), ``False`` if there is no live
        sequencer (caller does the pre-v0.79 direct send)."""
        seq = self._sequencers.get(engagement.id)
        if seq is None:
            return False
        # F3: DRAIN the relay to the completion block FIRST — wait until the
        # relay consumes the emit_completion consumption debt (⇒ every PRIOR
        # frame has been processed) so lagging prior-frame narration can never
        # post BELOW the completion message. Bounded (slot budget); on timeout
        # WARN and proceed. ``register_completion_consumption`` uses this same
        # request_id, so an emit_completion-driven finalize has a debt to await;
        # a cancel/error finalize has none and this returns immediately.
        rid = f"emit_completion:{engagement.id}"
        drained = await seq.await_completion_drain(rid)
        if not drained:
            logger.warning(
                "engagement %s: completion drain timed out — posting completion "
                "without a full relay drain (prior narration may lag)",
                engagement.id[:8],
            )
        await seq.flush_armed_intents()
        await seq.seal_narration()
        await seq.post_platform_notice(summary_text)
        return True

    def register_completion_consumption(
        self, engagement_id: str, args: dict,
    ) -> None:
        """§2 F1(c): register the emit_completion send INTENT (svc_casa_mcp
        ingress, hash = identity over raw args) as a one-block CONSUMPTION DEBT
        so the relay, reaching the emit_completion tool_use block, consumes it
        silently instead of emitting stray narration or binding a later
        same-hash intent. Best-effort — no live sequencer ⇒ no-op."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return
        from channels.output_sequencer import (
            EMIT_COMPLETION_TOOL, projection_hash as _pj,
        )
        rid = f"emit_completion:{engagement_id}"
        phash = _pj(EMIT_COMPLETION_TOOL, args if isinstance(args, dict) else {})
        intent, _created = seq.register_intent(
            request_id=rid, tool_name=EMIT_COMPLETION_TOOL,
            projection_hash=phash, poster=_completion_noop_poster,
        )
        # Mark the debt directly (no lock needed — a plain dataclass toggle; the
        # relay reads it under the lock at block-match time).
        intent.state = "posted"
        intent.timeout_posted = True
        intent.consumed = False

    async def reconcile_terminal_spool(self, engagement: EngagementRecord) -> None:
        """§3 terminal boot-reconciliation: a TERMINAL engagement whose spool
        file still holds pending receipts/notices (a drain that crashed / a
        send that failed pre-terminal) is drained here — posting to the topic
        if it still exists, else WARN-dropping (the topic is gone)."""
        ws = Path(self._engagements_root) / engagement.id
        spool_path = ws / _SPOOL_FILENAME
        if not spool_path.exists():
            return
        spool = _InboundSpool(
            engagement_id=engagement.id,
            spool_path=str(spool_path),
            write_fifo=lambda text: _never_deliver(),
            send_notice=lambda text, reply_to: self._spool_send_notice(
                engagement, text, reply_to),
        )
        if not spool.has_pending():
            return
        if engagement.topic_id is None:
            logger.warning(
                "engagement %s: terminal spool has pending receipts/notices "
                "but its topic is gone — dropping", engagement.id[:8],
            )
            spool.mark_all_settled()
            spool._persist_quiet()
            return
        await spool.drain()

    # -- v0.79.0 (§5) live-summary controller -------------------------------

    @staticmethod
    def _summary_goal_line(engagement: EngagementRecord) -> str:
        """The summary's stable title — the persisted SHORT topic title (W-R6,
        the SAME source the topic-name state edit reads). Legacy engagements
        with no persisted title fall back to the derived concise_task label so
        old records never crash."""
        title = getattr(engagement, "topic_title", "") or ""
        if title:
            return title
        try:
            from channels.state_emoji import concise_task
            return concise_task(engagement.task or "")
        except Exception:  # noqa: BLE001 — a goal line is best-effort
            return (engagement.task or "").strip()

    async def _post_initial_summary(self, engagement: EngagementRecord) -> int | None:
        """§5 boot: post the pinned live summary and persist its id. The pin
        itself is owned by ``ensure_pinned()`` (T4 F-4 — see below). Raises
        ``RuntimeError`` on a post failure so ``start`` aborts.

        A resumed/replayed engagement never calls this (it already has a
        persisted ``summary_message_id`` that the controller adopts on attach).
        """
        if engagement.topic_id is None:
            return None
        from drivers.summary_controller import STATUS_WORKING, render_summary
        text = render_summary(
            goal_line=self._summary_goal_line(engagement),
            status=STATUS_WORKING,
        )
        try:
            mid = await self._send_to_topic(engagement.topic_id, text)
        except Exception as exc:  # noqa: BLE001 — abort the launch
            raise RuntimeError(
                f"summary post failed for engagement {engagement.id[:8]}: {exc}"
            ) from exc
        if mid is None:
            raise RuntimeError(
                f"summary post returned no message id for engagement "
                f"{engagement.id[:8]}"
            )
        # Persist durably FIRST (so a restart resumes it), THEN set the
        # in-memory field. F7 (Sol r2): snapshot-BEFORE-mutate. The registry's
        # strict setter snapshots the record's PRE-mutation summary_message_id to
        # roll back on a persist failure. If we assigned ``engagement.summary_
        # message_id = mid`` HERE first — and ``engagement`` IS the registry's
        # own record object — the setter would snapshot the ALREADY-mutated new
        # id, making its rollback a no-op (a forced persist failure would leave
        # the new id in memory instead of the true prior value). So we let the
        # setter own the mutation+snapshot and only touch the in-memory field
        # AFTER a successful persist.
        setter = getattr(self._registry, "set_summary_message_id", None)
        if setter is not None:
            # §5 / F6: strict persistence — a summary posted but NOT persisted
            # cannot be resumed after a restart (§5's invariant would break), so
            # a persist failure ABORTS the launch (post-failure-aborts rule).
            try:
                await setter(engagement.id, mid)
            except Exception as exc:  # noqa: BLE001 — abort the launch
                raise RuntimeError(
                    f"summary_message_id persist failed for engagement "
                    f"{engagement.id[:8]}: {exc}"
                ) from exc
        # Set the in-memory record's field only after a durable persist (a no-op
        # when ``engagement`` already IS the registry record the setter mutated;
        # required when it is a distinct object or there is no registry setter).
        engagement.summary_message_id = mid
        # T4 F-4 (review): the initial pin attempt is NOT made here — the
        # per-engagement SummaryController doesn't exist yet at this point in
        # ``start()`` (``_ensure_summary`` only runs later, from
        # ``_spawn_background_tasks``), so any success here could never be
        # recorded on a controller and ``ensure_pinned()`` would immediately
        # redo the pin anyway once the controller is built. ``ensure_pinned()``
        # (scheduled as a task in ``_spawn_background_tasks``, retried on every
        # lifecycle flush) owns the pin attempt for both fresh and
        # resumed/replayed engagements alike.
        return mid

    async def adopt_summary_if_missing(self, engagement: EngagementRecord) -> None:
        """§5 / F7 adopt-on-attach migration: a LEGACY pre-v0.79 ACTIVE
        engagement replayed at boot has ``summary_message_id is None`` (it
        predates the pinned summary). Post + persist a summary NOW, on attach,
        so §5's invariant — no running engagement without a summary — holds
        before/immediately-at service start. Best-effort per record (boot replay
        isolates failures); a fresh v0.79 engagement (id already persisted) or a
        topic-less record is a no-op."""
        if (getattr(engagement, "summary_message_id", None) is not None
                or engagement.topic_id is None):
            return
        await self._post_initial_summary(engagement)

    def _ensure_summary(self, engagement: EngagementRecord) -> "SummaryController":
        """Build (or return) the per-engagement live-summary controller (§5)."""
        from drivers.summary_controller import SummaryController

        ctrl = self._summaries.get(engagement.id)
        if ctrl is None:
            reg = self._registry
            eid = engagement.id
            ctrl = SummaryController(
                engagement_id=eid,
                sequencer=self._ensure_sequencer(engagement),
                goal_line=self._summary_goal_line(engagement),
                # §A3: the pinned summary's ``Open questions:`` line reads the
                # EFFECTIVE unanswered set (persisted ``answered`` flag ∪ overlay).
                open_question_numbers=(lambda: self._effective_open_question_numbers(eid)),
                pin_message=(
                    (lambda mid: self._pin_topic_message(engagement.topic_id, mid))
                    if self._pin_topic_message is not None
                    and engagement.topic_id is not None
                    else None
                ),
                message_id=getattr(engagement, "summary_message_id", None),
            )
            self._summaries[eid] = ctrl
        return ctrl

    async def _summary_status_transition(
        self, engagement_id: str, status: str,
    ) -> None:
        """Acquire the next monotonic revision from the engagement-wide
        allocator and submit a lifecycle STATUS to the controller (§5). A
        lifecycle source (turn lifecycle / interaction_state / ask registry)
        calls this so the three sources are totally ordered."""
        ctrl = self._summaries.get(engagement_id)
        if ctrl is None:
            return
        rev: int | None = None
        alloc = getattr(self._registry, "allocate_summary_revision", None)
        if alloc is not None:
            try:
                rev = await alloc(engagement_id)
            except Exception as exc:  # noqa: BLE001 — degrade to no revision
                # T4 F-1 (review): this used to log at DEBUG only, so the drop
                # below (submit_status silently no-ops on revision=None) was
                # invisible in production. WARN — no fallback revision is
                # synthesized; the status transition for THIS call is dropped,
                # matching submit_status's existing "no revision, no update"
                # contract.
                logger.warning(
                    "engagement %s: allocate_summary_revision failed — "
                    "dropping this summary status transition (status=%s): %s",
                    engagement_id[:8], status, exc,
                )
        # F-EXPIRE (A2a) COERCION — the ONE funnel every lifecycle status source
        # already goes through (Sol r1-2). While operator-away, a working/waiting
        # submission is coerced to ⏸ paused so a compliant agent ending its turn
        # (``result`` → ⏳) cannot overwrite the paused line. Terminal statuses
        # bypass this funnel (finalize). A DIRECT STATUS_PAUSED submission
        # (``note_operator_away`` entry, Finding 3) passes through untouched —
        # the coercion only rewrites WAITING/WORKING.
        #
        # PINNED INVARIANT (Sol r2-3): sample ``operator_away`` AFTER the revision
        # allocation await returns. Sampling BEFORE it would let an inbound clear
        # + recompute land a correct status, only for THIS older suspended
        # coroutine to obtain a NEWER revision and write ⏸ back; sampling after
        # allocation guarantees any later clear allocates a strictly-newer
        # corrective revision that wins.
        from drivers.summary_controller import (
            STATUS_PAUSED, STATUS_WAITING_REPLY, STATUS_WORKING,
        )
        if (self._operator_away.get(engagement_id)
                and status in (STATUS_WAITING_REPLY, STATUS_WORKING)):
            status = STATUS_PAUSED
        await ctrl.submit_status(status, rev)

    async def note_ask_waiting(self, engagement_id: str) -> None:
        """W-R2: a successfully POSTED ask/anchor means the ball is now with the
        operator → ⏳ waiting for your reply. Driven from the ask LIFECYCLE (not
        the turn ``result``), because ``ask()`` blocks the subprocess for the
        whole time the operator owns the turn — so without this the summary
        would read ⚙️ working the entire wait. Acquires the next monotonic
        revision so it totally-orders against the settlement recompute below."""
        from drivers.summary_controller import STATUS_WAITING_REPLY
        await self._summary_status_transition(engagement_id, STATUS_WAITING_REPLY)

    # -- v0.83.0 (§A3) answered-overlay + effective open-question accessor ----
    def mark_answered_overlay(self, engagement_id: str, number: int) -> None:
        """Record an in-memory ANSWERED mark (§A3, Sol r6-4). Set by the answer
        path when ``mark_question_answered``'s strict persist RAISES — the live
        process then treats the question answered immediately (unioned onto the
        persisted flag) while later settle attempts retry the durable write."""
        self._answered_overlay.setdefault(engagement_id, set()).add(number)

    def _overlay_answered(self, engagement_id: str, number: int | None) -> bool:
        if number is None:
            return False
        return number in self._answered_overlay.get(engagement_id, set())

    def _effective_open_question_numbers(self, engagement_id: str) -> list[int]:
        """The A3 gates / pinned summary / recompute read UNANSWERED questions as
        ``persisted-unanswered MINUS overlay-answered MINUS reserved`` (Sol r6-4 +
        r7-1): a question whose ``answered`` write failed but is overlay-marked —
        or one whose answer is still only RESERVED (handler entry, pre-promotion)
        — stops gating immediately, before the durable flag lands."""
        reg = self._registry
        if reg is None or not hasattr(reg, "open_question_numbers"):
            return []
        try:
            nums = reg.open_question_numbers(engagement_id)
        except Exception:  # noqa: BLE001 — degrade to "no open questions"
            return []
        excluded = set(self._answered_overlay.get(engagement_id) or ())
        reserved_n = self._reserved_question_number(engagement_id)
        if reserved_n is not None:
            excluded.add(reserved_n)
        if not excluded:
            return list(nums)
        return [n for n in nums if n not in excluded]

    # -- v0.83.0 (§A3) answered-reservation token lifecycle -------------------
    def _reserved_question_number(self, engagement_id: str) -> int | None:
        """The question number currently RESERVED for this engagement (counts as
        answered for the effective/union view), or ``None``."""
        res = self._answer_reservations.get(engagement_id)
        return res[1] if res is not None else None

    def _oldest_unanswered_anchor(
        self, engagement_id: str, *, exclude_reserved: bool,
    ) -> dict | None:
        """The oldest open free-text anchor that is not yet answered — where
        "answered" is ``persisted flag ∪ overlay`` and, when ``exclude_reserved``,
        also ``∪ reserved``. Returns the RAW entry dict or ``None``.

        ``exclude_reserved=True`` is the RESERVE selection (never re-reserve an
        already-reserved anchor). ``exclude_reserved=False`` is the PROMOTION
        selection (a reserved anchor is exactly the one promotion consumes)."""
        reg = self._registry
        if reg is None:
            return None
        entries_getter = getattr(reg, "open_question_entries", None)
        if entries_getter is None:  # legacy registry without the raw accessor
            getter = getattr(reg, "oldest_open_anchor", None)
            return getter(engagement_id) if getter is not None else None
        reserved_n = (
            self._reserved_question_number(engagement_id)
            if exclude_reserved else None
        )
        anchors = [
            q for q in entries_getter(engagement_id)
            if q.get("kind") == "anchor"
            and not q.get("answered", False)
            and not self._overlay_answered(engagement_id, q.get("n"))
            and q.get("n") != reserved_n
        ]
        if not anchors:
            return None
        return min(anchors, key=lambda q: q.get("n", 0))

    # -- v0.83.0 (§A3(a)+(c)) ask-maintenance lock + ingress marker ----------
    def ask_maintenance_lock(self, engagement_id: str) -> asyncio.Lock:
        """The per-engagement ASK-MAINTENANCE lock (create-on-demand). PINNED
        DISCIPLINE: only ever taken on driver/handler tasks with the SEQUENCER
        lock NOT held — the relay-invoked poster (which runs under the sequencer
        lock) NEVER acquires it. It serializes the ask ingress reservation
        (pending-predicate check + marker set) against the reply gate's check."""
        lock = self._ask_maint_locks.get(engagement_id)
        if lock is None:
            lock = asyncio.Lock()
            self._ask_maint_locks[engagement_id] = lock
        return lock

    def ask_inflight(self, engagement_id: str) -> str | None:
        """The request_id of an ask that passed its gates but is not yet durable
        (broker-registered / add_open_question'd), or ``None``. Read under the
        maintenance lock by the ingress reservation + reply gate predicates."""
        return self._ask_inflight.get(engagement_id)

    def set_ask_inflight(self, engagement_id: str, request_id: str) -> None:
        """Claim the ingress marker for ``request_id`` (called under the
        maintenance lock, right after the pending predicate passes)."""
        self._ask_inflight[engagement_id] = request_id

    def clear_ask_inflight(
        self, engagement_id: str, request_id: str | None = None,
    ) -> None:
        """Clear the ingress marker — CAS on ``request_id`` (clear only when the
        marker still belongs to this request, so a terminal-failure backstop can
        never clobber a newer ask's marker). ``request_id=None`` clears
        unconditionally. Cleared WITHOUT the maintenance lock (asyncio
        run-to-completion makes the sync marker→durable handoff gap-free)."""
        if request_id is None or self._ask_inflight.get(engagement_id) == request_id:
            self._ask_inflight.pop(engagement_id, None)

    def effective_open_anchor(self, engagement_id: str) -> dict | None:
        """The oldest UNANSWERED free-text anchor (effective view — persisted
        ``answered`` ∪ overlay ∪ reserved excluded), or ``None``. The A3 reply /
        stacking gates' unanswered-anchor clause."""
        return self._oldest_unanswered_anchor(engagement_id, exclude_reserved=True)

    async def mark_send_intent_compensated(
        self, engagement_id: str, request_id: str, message_id: int,
    ) -> Any:
        """§A3(c) (Sol r5-5/r6-1): the ``mark_send_intent_posted``-adjacent
        COMPENSATED seam. The initial-anchor poster calls this on an
        ``add_open_question`` failure AFTER the wire post: the sequencer advances
        high-water to ``message_id`` and resolves the intent exactly once as
        ``{"ok": False, "message_id": message_id, "compensated": True}`` — the
        physical write is accounted, the logical result is failure."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return None
        return await seq.mark_intent_compensated(request_id, message_id)

    async def settle_answered_anchor(
        self, engagement_id: str, number: int,
    ) -> int | None:
        """§A3(c) post-add generation re-check: an operator envelope that arrived
        between the ingress reservation and ``add_open_question`` IS this anchor's
        answer. Mark it answered (strict; overlay on raise — Task-6 policy) and
        run the shared visual settle. NOT maintenance-locked — the caller is the
        relay poster under the sequencer lock (settle stays lock-free here)."""
        reg = self._registry
        if reg is None:
            return None
        engagement = reg.get(engagement_id)
        if engagement is None:
            return None
        mark = getattr(reg, "mark_question_answered", None)
        if mark is not None:
            try:
                await mark(engagement_id, number)
            except Exception:  # noqa: BLE001 — overlay covers the live process
                self.mark_answered_overlay(engagement_id, number)
        # DEADLOCK NOTE (B2): this runs on the relay poster task UNDER the
        # sequencer lock, so it MUST stay lock-free — acquiring the maintenance
        # lock here would deadlock a concurrent re-anchor that holds maintenance
        # and is blocked on the sequencer lock at ``post_discrete``. Safe without
        # the lock: the entry was JUST marked answered above, so a racing
        # re-anchor's step-2 revalidate (which itself blocks on the sequencer lock
        # WE hold) declines and never posts a competing copy. Re-read fresh by
        # number and settle the CURRENT copy.
        amid = await self._settle_anchor_entry_locked(engagement, number)
        try:
            await self.recompute_engagement_status(engagement_id)
        except Exception:  # noqa: BLE001 — status recompute is advisory
            logger.debug("recompute after answered-anchor settle failed",
                         exc_info=True)
        return amid

    def reserve_answer(self, engagement_id: str) -> str | None:
        """§A3 (Sol r7-1): reserve the oldest UNANSWERED anchor as answered by
        the current operator message, carrying a unique token. Returns the token,
        or ``None`` when no unanswered anchor is open (nothing to reserve — the
        message still enqueues; promotion is unconditional). Called SYNCHRONOUSLY
        at Telegram handler entry, in the same section as the high-water advance
        (no await between), so a concurrent result-finalize that observed the
        answer's high-water also observes the reservation."""
        anchor = self._oldest_unanswered_anchor(
            engagement_id, exclude_reserved=True)
        if anchor is None:
            return None
        n = anchor.get("n")
        if n is None:
            return None
        token = uuid.uuid4().hex
        self._answer_reservations[engagement_id] = (token, n)
        return token

    async def rollback_answer_reservation(
        self, engagement_id: str, token: str | None,
        *, suppress_reanchor: bool = False,
    ) -> bool:
        """§A3 (Sol r7-1/r9-1): CAS-roll-back a reservation — clear it ONLY when
        the CURRENT reservation still carries ``token`` (never clobber a later
        message's reservation) AND it was not already promoted (promotion deletes
        the entry, so a consumed reservation is simply absent → CAS fails).
        Returns ``True`` on a successful clear. On success calls the Task-9 seam
        ``_on_reservation_rolled_back`` (a last-reservation-clearing rollback
        schedules a compensating re-anchor pass there).

        F2 (whole-branch gate): a TERMINAL command (``/cancel``/``/complete``)
        rolls back the reservation and then FINALIZES the engagement, whose
        ``settle_all_open_questions`` owns every open anchor. Firing the
        fourth-consumer re-anchor pass here would post a redundant anchor copy
        that terminal settlement immediately settles. When ``suppress_reanchor``
        is set, the CAS clear still happens but the re-anchor latch is NEITHER
        set NOR consumed — the imminent terminal settle owns the entries."""
        if token is None:
            return False
        res = self._answer_reservations.get(engagement_id)
        if res is None or res[0] != token:
            return False
        del self._answer_reservations[engagement_id]
        if not suppress_reanchor:
            await self._on_reservation_rolled_back(engagement_id)
        return True

    async def _on_reservation_rolled_back(self, engagement_id: str) -> None:
        """§A3(b) consumer (d) — the FOURTH turn boundary (Sol r10-1/r12-1): a CAS
        rollback that cleared a still-un-promoted reservation lands here. An IDLE
        engagement whose operator message did NOT become a delivered turn
        (``/silent``, a rejected originator-only command, a handler cancellation,
        a spool-rejected message) gets no later ``result``, abnormal spawn, or
        terminal finalize — so the rollback path itself, AFTER any platform notice
        it already posted (Sol r12-1 ordering), directly runs the same idempotent,
        revalidated, maintenance-locked re-anchor pass so the anchor still ends up
        LAST. SET the latch first (case b: a rollback CAS cleared the last
        reservation) so a failed pass leaves the obligation armed for the retry
        owner / a racing boundary consumer (harmlessly — one finds it consumed)."""
        reg = self._registry
        engagement = reg.get(engagement_id) if reg is not None else None
        if engagement is None:
            return
        self.set_reanchor_due(engagement_id)
        await self._consume_reanchor(engagement)

    async def _promote_answer_on_enqueue(
        self, engagement: EngagementRecord,
    ) -> int | None:
        """§A3 (Sol r9-1): PROMOTE the answer at a durable non-initial enqueue.
        UNCONDITIONAL — any delivered operator message answers the one open
        question, regardless of which token (if any) holds the reservation:
        mark the oldest unanswered anchor answered (``mark_question_answered``
        strict; on raise → overlay per Task-6 policy), run the SAME visual settle
        the delivery-time path uses, and CONSUME the reservation. Returns the
        anchor's tg_message_id (recorded on the envelope so delivery only
        threads), or ``None`` when no anchor is open."""
        eid = engagement.id
        anchor = self._oldest_unanswered_anchor(eid, exclude_reserved=False)
        # Consume the reservation regardless of whether an anchor was found —
        # promotion is the reservation's terminal state (Sol r9-1).
        self._answer_reservations.pop(eid, None)
        # §A3(b) latch-clear discipline (Sol r13-1): PROMOTION (the question got
        # answered) is one of the three latch-clearing events — discharge the
        # re-anchor obligation and retire the retry owner.
        self._reanchor_due.discard(eid)
        self._retire_reanchor_retry(eid)
        if anchor is None:
            return None
        n = anchor.get("n")
        reg = self._registry
        mark = getattr(reg, "mark_question_answered", None) if reg else None
        if mark is not None and n is not None:
            try:
                await mark(eid, n)
            except Exception:  # noqa: BLE001 — strict persist failed
                # §A3 answered-persist-failure policy (Sol r6-4/r7-2): the
                # envelope is already durably spooled, so the question must not
                # keep gating. The overlay covers the live process; the strict
                # write retries at each later settle attempt.
                self.mark_answered_overlay(eid, n)
        # Visual settle for THIS anchor — shared with the delivery-time path.
        return await self._settle_open_anchor(engagement, anchor=anchor)

    async def _settle_ledger_entry(
        self, rec: EngagementRecord, q: dict, *, answered_suffix: bool,
        override_suffix: str | None = None,
    ) -> int | None:
        """Settle ONE open-question entry's CURRENT copy plus every staged
        ``stale_mids`` copy, honoring the entry-removal invariant (§A3, Sol r5-3):
        the entry is REMOVED only when the current settle edit is CONFIRMED AND no
        stale copy remains; otherwise it PERSISTS (its ``answered`` flag keeps it
        invisible to the gates/summary) with each confirmed stale copy un-staged.

        ``answered_suffix`` picks the terminal copy (``✅ answered below`` vs
        ``⌛ expired``). Independently, when the entry is overlay-answered but its
        durable ``answered`` flag never landed, the strict persist is RETRIED here
        (Sol r6-4/r7-2 — convergence at each later settle attempt). Returns the
        entry's current ``tg_message_id`` (for anchor reply-threading)."""
        reg = self._registry
        n = q.get("n")
        # Converge the answered flag when an earlier strict persist failed
        # (overlay covers the live process; retry before the visual settle).
        if (reg is not None and n is not None
                and not q.get("answered", False)
                and self._overlay_answered(rec.id, n)):
            mark = getattr(reg, "mark_question_answered", None)
            if mark is not None:
                try:
                    await mark(rec.id, n)
                except Exception:  # noqa: BLE001 — retry again at the next settle
                    logger.debug(
                        "mark_question_answered retry failed (n=%s)", n,
                        exc_info=True)
        if override_suffix is not None:
            suffix = override_suffix
        else:
            suffix = (
                _OPEN_Q_ANSWERED_SUFFIX if answered_suffix
                else _OPEN_Q_EXPIRED_SUFFIX
            )
        display = q.get("text") or (f"Q{n}:" if n is not None else "")
        text = f"{display}{suffix}"
        cur_mid = q.get("tg_message_id")
        current_confirmed = await self._confirm_settle_mid(rec, cur_mid, text, n)
        # A8 · Q1-settle observability: one INFO line per CONFIRMED settle (a real
        # keyboard-clearing edit landed). The outcome mirrors the settle copy.
        if current_confirmed and cur_mid is not None:
            if override_suffix == _OPEN_Q_CANCELLED_SUFFIX:
                _outcome = "cancelled"
            elif answered_suffix:
                _outcome = "answered"
            else:
                _outcome = "expired"
            logger.info(
                "ask settle CONFIRMED (eng=%s q=%s mid=%s outcome=%s)",
                rec.id[:8], n if n is not None else "-", cur_mid, _outcome)
        # Every staged stale copy is settled with the same confirmed-gate; a
        # confirmed one is un-staged, an unconfirmed one keeps the entry present.
        remaining_stale: list[int] = []
        unstage = getattr(reg, "unstage_stale_mid", None) if reg is not None else None
        for smid in list(q.get("stale_mids") or []):
            if await self._confirm_settle_mid(rec, smid, text, n):
                if unstage is not None and n is not None:
                    try:
                        await unstage(rec.id, n, smid)
                    except Exception:  # noqa: BLE001
                        # M4: a confirmed stale-copy edit whose STRICT unstage
                        # persist RAISES must NOT let the entry close with a
                        # durable stale_mid still staged — keep the mid in
                        # ``remaining_stale`` so no close happens this pass and
                        # boot/settle reconciliation retries it.
                        logger.warning(
                            "engagement %s: unstage_stale_mid persist failed "
                            "(n=%s mid=%s) — retaining entry", rec.id[:8], n,
                            smid, exc_info=True)
                        remaining_stale.append(smid)
            else:
                remaining_stale.append(smid)
        # Entry-removal invariant: remove ONLY when the current copy is confirmed
        # AND no stale copy remains; otherwise the entry persists.
        if current_confirmed and not remaining_stale:
            close = getattr(reg, "close_open_question", None) if reg is not None else None
            if close is not None and n is not None:
                try:
                    await close(rec.id, n)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "engagement %s: close_open_question failed (n=%s)",
                        rec.id[:8], n, exc_info=True)
        return cur_mid

    async def _confirm_settle_mid(
        self, rec: EngagementRecord, mid: int | None, text: str, n: int | None,
    ) -> bool:
        """Confirmed-settle ONE message id (§A3). ``True`` when there is nothing to
        settle (``mid`` is None) OR the edit is confirmed; ``False`` fail-closed
        when a message-backed copy has no edit primitive, or the bounded retry
        stays unconfirmed — leaving the ledger entry intact for a later pass."""
        if mid is None:
            return True
        if self._edit_topic_message is None:
            logger.warning(
                "engagement %s: open-question settle has a message id but no edit "
                "primitive (n=%s) — leaving ledger entry INTACT", rec.id[:8], n)
            return False
        settled = await confirmed_settle_edit(
            lambda mid=mid, text=text: self._edit_topic_message(
                rec.topic_id, mid, text, clear_keyboard=True),
            sleep=self._sleep,
        )
        if not settled:
            logger.warning(
                "engagement %s: open-question settle UNCONFIRMED after retries "
                "(n=%s) — leaving ledger entry INTACT", rec.id[:8], n)
        return settled

    async def recompute_engagement_status(self, engagement_id: str) -> None:
        """W-R2: on ask/anchor SETTLEMENT, recompute the summary status from the
        REMAINING open questions — stay ⏳ waiting while any question is still
        open; return to ⚙️ working only when none remain AND the turn is still
        running. A terminal status stays absolute (``submit_status`` rejects any
        later transition). Each transition acquires a fresh revision, so the
        linearization pin (registration → waiting → THEN settlement) guarantees
        a fast tap during the post window can never leave the summary
        stuck-waiting: the recompute's revision is always allocated after the
        waiting submission's."""
        from drivers.summary_controller import (
            STATUS_WAITING_REPLY, STATUS_WORKING,
        )
        # §A3: read the EFFECTIVE unanswered set (persisted flag ∪ overlay), so an
        # answered-but-unconfirmed-settle question stops holding the summary ⏳.
        open_qs = self._effective_open_question_numbers(engagement_id)
        if open_qs:
            await self._summary_status_transition(
                engagement_id, STATUS_WAITING_REPLY)
        elif self._turn_running.get(engagement_id):
            await self._summary_status_transition(
                engagement_id, STATUS_WORKING)
        # F1 (Sol diff gate): the open-questions SET may have changed with NO
        # status-class transition — one of several questions settled while the
        # summary stays ⏳ waiting, or none remain but the turn already ended
        # (neither branch above fires). Force a summary refresh so the pinned
        # open-questions line reflects the remaining set; the no-op gate elides
        # a redundant edit when a transition above already reflowed it.
        ctrl = self._summaries.get(engagement_id)
        if ctrl is not None:
            try:
                await ctrl.refresh()
            except Exception:  # noqa: BLE001 — summary refresh is advisory
                logger.debug(
                    "summary refresh after status recompute failed",
                    exc_info=True)

    async def finalize_summary(
        self, engagement: EngagementRecord, outcome: str,
    ) -> None:
        """§5 engagement finalize: set the TERMINAL summary status (absolute),
        cancel the tick and perform the mandatory finalize flush. Called by
        ``_finalize_engagement`` while the topic is still open."""
        from drivers.summary_controller import OUTCOME_STATUS, STATUS_ERROR
        ctrl = self._summaries.get(engagement.id)
        if ctrl is None:
            return
        await ctrl.finalize(OUTCOME_STATUS.get(outcome, STATUS_ERROR))

    def _ensure_sequencer(self, engagement: EngagementRecord) -> "OutputSequencer":
        """Build (or return) the per-engagement OUTPUT SEQUENCER (§2).

        Wraps the driver's own relay send/edit primitives so the sequencer, the
        topic-stream relay and the discrete ingresses all drive ONE serialized
        writer with a single high-water mark and intent registry.
        """
        from channels.output_sequencer import OutputSequencer

        seq = self._sequencers.get(engagement.id)
        if seq is None:
            seq = OutputSequencer(
                engagement_id=engagement.id,
                topic_id=engagement.topic_id,
                send_message=self._relay_send_message,
                edit_message=self._relay_edit_message,
                # A9: markup-capable wire for post_discrete/edit_discrete.
                send_message_markup=self._relay_send_message_markup,
                edit_message_markup=self._relay_edit_message_markup,
            )
            self._sequencers[engagement.id] = seq
        return seq

    # -- v0.79.0 (§2) discrete-posting intent-registration API (T2/T3 seam) --

    def register_send_intent(
        self, *, engagement_id: str, request_id: str, tool_name: str,
        projection_hash: str, poster: Any,
    ) -> Any:
        """Register (or idempotently reattach to) a discrete-send INTENT (§2(1)).

        The T2/T3 ingress adapters call this at fence entry. ``poster`` is an
        ``async () -> int | None`` that performs the actual keyboard/text post
        (the sequencer invokes it when the relay reaches the matching content
        block). Returns ``(intent, created)`` or ``None`` if the engagement has
        no live sequencer. A same-``request_id`` call reattaches idempotently
        and the caller can read the recorded outcome via ``send_intent_outcome``.
        """
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return None
        return seq.register_intent(
            request_id=request_id, tool_name=tool_name,
            projection_hash=projection_hash, poster=poster,
        )

    def set_send_intent_poster(
        self, engagement_id: str, request_id: str, poster: Any,
    ) -> Any:
        """Install the REAL relay-invoked poster on a registered intent (§2(3),
        T3). The ask/reply ingress registers early (reattach idempotency), then
        sets the poster and arms; the relay posts it at its tool_use block."""
        seq = self._sequencers.get(engagement_id)
        return seq.set_intent_poster(request_id, poster) if seq is not None else None

    def arm_send_intent(self, engagement_id: str, request_id: str) -> Any:
        """Move a pending intent to ``armed`` — the point of no return (§2(2))."""
        seq = self._sequencers.get(engagement_id)
        return seq.arm_intent(request_id) if seq is not None else None

    def cancel_send_intent(self, engagement_id: str, request_id: str) -> Any:
        """Cancel a pending/armed intent → tombstone (§2(2))."""
        seq = self._sequencers.get(engagement_id)
        return seq.cancel_intent(request_id) if seq is not None else None

    def record_send_intent_refusal(
        self, engagement_id: str, request_id: str, outcome: dict,
    ) -> Any:
        """A2 (Finding 1): tombstone the intent AND record a refusal outcome so a
        same-request_id retry reattaches to the SAME refusal. See
        ``OutputSequencer.record_intent_refusal``."""
        seq = self._sequencers.get(engagement_id)
        return seq.record_intent_refusal(request_id, outcome) if seq is not None else None

    def record_send_intent_cancelled_nowait(
        self, engagement_id: str, request_id: str, outcome: dict,
    ) -> bool:
        """A3 · F-ORDER (Sol A3 wave 4): FULLY SYNCHRONOUS transport-cancellation
        cleanup against an in-flight relay post — NO await, so a second
        ``Task.cancel()`` cannot interrupt it mid-flight (the double-cancel window
        the awaited wave-3 predecessor left open). Reads ``posting`` synchronously:
        if a relay post is in flight / resolved the cancel LOSES (returns ``False``
        — the poster owns the marker + outcome, never clobbered), otherwise
        tombstones + records the ``cancelled`` outcome ONLY while the intent is
        still cancellable (returns ``True`` — the caller then clears the ingress
        marker). See ``OutputSequencer.record_intent_cancelled_nowait``."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return False
        return seq.record_intent_cancelled_nowait(request_id, outcome)

    def send_intent_outcome(self, engagement_id: str, request_id: str) -> Any:
        """Recorded outcome (incl. posted message id) for a reattaching retry."""
        seq = self._sequencers.get(engagement_id)
        return seq.intent_outcome(request_id) if seq is not None else None

    async def mark_send_intent_posted(
        self, engagement_id: str, request_id: str, message_id: int | None,
    ) -> Any:
        """Record a discrete ingress' out-of-band post (§2(5) consumption debt +
        reattach outcome). See ``OutputSequencer.mark_intent_posted``."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return None
        return await seq.mark_intent_posted(request_id, message_id)

    async def await_send_intent(
        self, engagement_id: str, request_id: str, timeout: float | None = None,
    ) -> Any:
        """F3 fail-closed: block until the deferred intent posts (ok) or fails
        (ok:false), bounded by the sequencer's transport budget. Returns the
        recorded outcome dict, or ``None`` if there is no live sequencer /
        intent (the caller treats that as a non-post). See
        ``OutputSequencer.await_intent_resolution``."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return None
        return await seq.await_intent_resolution(request_id, timeout)

    async def advance_topic_high_water_for_inbound(
        self, engagement_id: str, operator_msg_id: int | None = None,
    ) -> None:
        """§2: an inbound operator message advances the high-water mark and
        SEALS open narration. (The inbound-spool call site is wired by T2.)"""
        seq = self._sequencers.get(engagement_id)
        if seq is not None:
            await seq.advance_high_water_for_inbound(operator_msg_id)

    # -- v0.79.0 (§4) ask inbound-gate reads + refusal escalation ----------

    def inbound_generation(self, engagement_id: str) -> int:
        """§4 gate: the current operator-message generation for the ask
        race-close re-check. 0 when no spool exists (degraded / no driver)."""
        spool = self._inbound.get(engagement_id)
        return spool.generation() if spool is not None else 0

    def inbound_unread_depth(self, engagement_id: str) -> int:
        """§4 gate: number of unseen queued operator messages. ``> 0`` refuses
        the ask. 0 when no spool exists."""
        spool = self._inbound.get(engagement_id)
        return spool.unread_depth() if spool is not None else 0

    def record_ask_refusal(self, engagement_id: str) -> int:
        """§4: bump + return the count of consecutive ask refusals THIS turn.
        From the 3rd, log a WARN counter (observability for a future hard
        turn-end primitive — OUT of scope here; the escalated copy is the only
        mechanism)."""
        n = self._ask_refusals.get(engagement_id, 0) + 1
        self._ask_refusals[engagement_id] = n
        if n >= 3:
            logger.warning(
                "engagement %s: %d consecutive ask refusals this turn — the "
                "agent keeps asking while an operator message is unread "
                "(no hard force-end primitive exists; soft escalation only)",
                engagement_id[:8], n,
            )
        return n

    # -- F-EXPIRE (A2a) operator-away suspend state ------------------------

    async def note_operator_away(self, engagement_id: str, gen: int) -> bool:
        """F-EXPIRE ENTER (generation-CAS, Sol r1-3/r2-2): mark the engagement
        operator-away ONLY IF the inbound generation still equals ``gen`` — the
        value sampled at the ask's entry and carried in the broker meta as
        ``inbound_gen``. A racing inbound that already bumped the generation
        FAILS the CAS, so a lost-response retry reattaching after an inbound
        cleared the away state can never re-wedge it with a fresher generation.
        Returns whether the flag was set. In-memory only (no persistence).

        Sol A2 review (Finding 3): on a SUCCESSFUL CAS, DRIVE the paused summary
        DIRECTLY — the timeout settle edit that would otherwise recompute the
        status may be unconfirmed (``_close_question``'s recompute never runs),
        leaving the engagement showing ⏳ forever while suspended. Submitting
        STATUS_PAUSED through the ONE ``_summary_status_transition`` funnel (a
        fresh monotonic revision) makes ⏸ appear regardless; the funnel's
        away-coercion + monotonic revision make a redundant/racing submit
        idempotent."""
        if self.inbound_generation(engagement_id) != gen:
            return False
        self._operator_away[engagement_id] = True
        from drivers.summary_controller import STATUS_PAUSED
        try:
            await self._summary_status_transition(engagement_id, STATUS_PAUSED)
        except Exception:  # noqa: BLE001 — the away flag stands regardless
            logger.debug(
                "paused-status submit after operator-away entry failed",
                exc_info=True)
        return True

    def operator_away_active(self, engagement_id: str) -> bool:
        """F-EXPIRE gate: True while the engagement is SUSPENDED waiting for the
        operator (set on ask expiry, cleared on the next durable inbound)."""
        return self._operator_away.get(engagement_id, False)

    def record_away_refusal(self, engagement_id: str) -> int:
        """F-EXPIRE backstop counter: bump + return the count of consecutive
        operator-away ask refusals in the current away-episode. Task 5's
        force-turn-boundary backstop CONSUMES this count; nothing here
        force-ends. Reset when the away state clears (``_clear_operator_away``).

        A2b HARD BACKSTOP: refusals are instant, so a doctrine-defying agent
        could loop ask→refusal at token speed. On the 2nd refusal in an episode
        (``_away_suspend_fired`` gates re-firing) the driver force-ends the CLI
        turn ONCE via the verified group-kill. The trigger lives here — the
        handler-side gate already routes every refusal through this method, so
        channel_handlers needs no further change."""
        n = self._away_refusals.get(engagement_id, 0) + 1
        self._away_refusals[engagement_id] = n
        # F3: after an unverified outcome the guard re-arms but a monotonic
        # cooldown must elapse before it may re-fire — else an ask→refusal loop
        # churns probes/subprocesses at token speed.
        cooldown_until = self._away_force_cooldown_until.get(engagement_id, 0.0)
        if (
            n >= 2
            and engagement_id not in self._away_suspend_fired
            and self._monotonic() >= cooldown_until
        ):
            self._away_suspend_fired.add(engagement_id)
            self._trigger_force_suspend(engagement_id)
        return n

    def _trigger_force_suspend(self, engagement_id: str) -> None:
        """A2b: mark the current epoch expected-terminated BEFORE signalling (so
        ``_log_abnormal_exit`` annotates the ensuing respawn), then spawn the
        verified group-kill as a tracked background task. Degrades to a no-op if
        no event loop is running (a sync fake context)."""
        # Whole-branch gate r3: SINGLE-FLIGHT — never overwrite a live owner
        # (a cancelled-but-not-yet-done predecessor must stay visible to the
        # drain until its done callback retires it; overwriting would hide it
        # from both drain surfaces for one loop turn). Checked BEFORE creating
        # a duplicate task so no side-effecting coroutine ever starts.
        existing = self._force_tasks.get(engagement_id)
        if existing is not None and not existing.done():
            logger.debug(
                "force_turn_boundary: kill already in flight for %s — "
                "deferring to it", engagement_id[:8])
            return
        # Stamp the epoch first — the kill races the respawn's spawn event.
        self._forced_suspend_epochs[engagement_id] = self._epoch_pending.get(
            engagement_id)
        try:
            task = asyncio.create_task(
                self._run_force_suspend(engagement_id),
                name=f"force_suspend:{engagement_id[:8]}")
        except RuntimeError:  # no running loop — cannot schedule the kill
            logger.debug(
                "force_turn_boundary: no running loop; skipping force-end "
                "for %s", engagement_id[:8])
            # Sol A2 wave-3, Finding 1: nothing fired → RE-ARM so a later refusal
            # (with a loop) can retry rather than staying permanently latched.
            self._away_suspend_fired.discard(engagement_id)
            return
        # A2b (Sol A2 review): hold a DEDICATED per-engagement handle (replacing
        # the anonymous ``_tasks`` append) so ``_clear_operator_away`` can cancel
        # this specific kill on operator re-engagement. Retire the handle when
        # the task completes so a stale/cancelled reference is never re-cancelled.
        self._force_tasks[engagement_id] = task
        task.add_done_callback(
            lambda t, eid=engagement_id: (
                self._force_tasks.pop(eid, None)
                if self._force_tasks.get(eid) is t else None))

    def _register_force_cleanup(
        self, engagement_id: str, task: asyncio.Task,
    ) -> None:
        """Adopt ``force_turn_boundary``'s shielded post-signal cleanup task when
        the force-suspend task is cancelled mid-kill (operator returned after
        SIGTERM was sent).

        Sol A2 wave-3, Finding 2: the cleanup is CANCEL-EXEMPT — it must complete
        its extinction poll + SIGKILL escalation (bounded ≤ ~7 s) even under
        engagement teardown, or a SIGTERM-resistant MCP/tool child survives while
        verification is skipped. It therefore goes in the DEDICATED
        ``_force_cleanups`` set that ``cancel()`` NEVER cancels — NOT
        ``_tasks[eng_id]`` (wave-2's bug): ``cancel()`` iterates+cancels
        ``_tasks`` and, having already popped ``_tasks[eng_id]``, a handoff onto
        it would (a) risk cancelling the kill and (b) resurrect a stale, never-
        reaped ``_tasks`` entry after teardown. The ``add_done_callback`` retires
        the task from the set on completion so no reference lingers.

        Process shutdown bounded-awaits these via :meth:`drain_force_cleanups`
        (F1 whole-branch gate) so a resistant engagement subprocess is verified
        extinct before the process exits; each is otherwise self-limiting."""
        self._force_cleanups.add(task)
        task.add_done_callback(self._force_cleanups.discard)

    async def drain_force_cleanups(self, timeout: float = 10.0) -> bool:
        """F1 (whole-branch gate): bounded, LOOP-until-stable shutdown drain of
        the driver's force-suspend machinery across BOTH surfaces:

          * ``_force_tasks`` — the still-running ``_run_force_suspend`` OWNERS. A
            post-SIGTERM cleanup normally lives INSIDE its owner (shield-awaited
            there); it only MIGRATES to ``_force_cleanups`` when the owner is
            cancelled (operator returned mid-kill). So an in-flight owner is
            itself an un-drained cleanup and must be awaited here.
          * ``_force_cleanups`` — the CANCEL-EXEMPT shielded extinction-poll +
            SIGKILL escalation handed off by ``force_turn_boundary`` (≤ ~7 s each).

        These cleanups are exempt from ``cancel()`` precisely so a SIGTERM-
        resistant MCP/tool child is verified gone (or SIGKILL-escalated) rather
        than orphaned — but process shutdown must actually WAIT for that
        verification, or teardown races the SIGKILL and the resistant subprocess
        outlives the container.

        The drain LOOPS within a single ``timeout`` budget: each iteration
        RE-SNAPSHOTS both surfaces and awaits whatever is pending. Re-snapshotting
        is what makes it stable — an owner cancelled mid-drain hands a fresh
        cleanup off AFTER an earlier snapshot, and the loop's next iteration
        catches it instead of letting it slip past. It settles when both surfaces
        are empty (``True``) or the budget is exhausted (``False``).

        casa_core's shutdown sequence calls this AFTER channel + HTTP ingress
        teardown so the drain point is INGRESS-QUIESCENT — no inbound can spawn a
        fresh force-suspend or fire an operator-away clear that hands a new
        cleanup off once the loop has settled.

        Returns ``True`` when both surfaces drained within ``timeout``; ``False``
        on budget exhaustion (WARN with the still-pending owner + cleanup counts)
        so shutdown proceeds truthfully rather than blocking forever. NEVER
        raises — a drain failure must not wedge shutdown."""
        deadline = self._monotonic() + timeout
        while True:
            owners = [t for t in self._force_tasks.values() if not t.done()]
            cleanups = [t for t in self._force_cleanups if not t.done()]
            pending = owners + cleanups
            if not pending:
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                logger.warning(
                    "drain_force_cleanups: %d force-suspend owner(s) + %d "
                    "cleanup(s) did not finish within %.1fs — proceeding with "
                    "shutdown", len(owners), len(cleanups), timeout)
                return False
            try:
                await asyncio.wait(pending, timeout=remaining)
            except Exception:  # noqa: BLE001 — shutdown must complete regardless
                logger.warning(
                    "drain_force_cleanups: wait raised", exc_info=True)
                return False
            # Loop: re-snapshot to catch a handoff that landed during this wait.

    async def _run_force_suspend(self, engagement_id: str) -> None:
        """A2b: await the injected verified group-kill and log the truthful
        outcome. A False (unverified) is WARN-logged; the kill helper never
        touches s6 wanted-state, so s6 auto-respawns the run script into the
        FIFO-blocked suspended state either way."""
        try:
            ok = await self._force_turn_boundary(
                engagement_id=engagement_id,
                workspace_dir=str(Path(self._engagements_root) / engagement_id),
                expected_epoch=self._forced_suspend_epochs.get(engagement_id),
                track_task=(
                    lambda t, eid=engagement_id:
                    self._register_force_cleanup(eid, t)),
            )
        except asyncio.CancelledError:
            # A2b: operator returned mid-kill (``_clear_operator_away`` cancelled
            # this task). Sol A2 wave-2 Finding 3: if SIGTERM was already sent,
            # ``force_turn_boundary`` has already handed its shielded post-signal
            # cleanup to ``_register_force_cleanup`` so the SIGKILL escalation
            # still completes under teardown ownership; a pre-signal cancel left
            # nothing running. Either way the operator's own message provides the
            # turn boundary — nothing to log, propagate the cancellation.
            raise
        except Exception:  # noqa: BLE001 — the backstop must never wedge the driver
            logger.warning(
                "engagement %s: force_turn_boundary raised", engagement_id[:8],
                exc_info=True)
            # Sol A2 wave-3, Finding 1: an unverified (raised) outcome RE-ARMS so
            # the NEXT away-refusal retries — a transient failure must not
            # permanently disable the once-per-episode backstop. F3: pace the
            # retry behind a monotonic cooldown so the loop cannot churn.
            self._away_force_cooldown_until[engagement_id] = (
                self._monotonic() + _AWAY_FORCE_COOLDOWN_S)
            self._away_suspend_fired.discard(engagement_id)
            return
        if ok:
            # Verified suspended → LATCH (leave the guard set) so the episode does
            # not force-end again.
            logger.info(
                "engagement %s: forced turn boundary (operator away, 2nd "
                "refusal) — verified suspended", engagement_id[:8])
        else:
            logger.warning(
                "engagement %s: forced turn boundary NOT verified — agent may "
                "still be looping", engagement_id[:8])
            # Sol A2 wave-3, Finding 1: an unverified (False) outcome RE-ARMS the
            # once-per-episode guard so a subsequent away-refusal fires again
            # (e.g. a transient ``unknown`` probe cleared on the next attempt).
            # F3: pace the re-fire behind a monotonic cooldown to bound churn.
            self._away_force_cooldown_until[engagement_id] = (
                self._monotonic() + _AWAY_FORCE_COOLDOWN_S)
            self._away_suspend_fired.discard(engagement_id)

    async def _clear_operator_away(self, engagement_id: str) -> None:
        """F-EXPIRE EXIT: a durably-enqueued operator message ends the away
        episode. Clear the flag + the away-refusal counter, THEN recompute the
        summary status so the ⏸ coercion window closes crisply — the recompute
        allocates a strictly-newer revision that lands the correct ⚙️/⏳. A no-op
        (no recompute) when the engagement was not away, so an ordinary operator
        message never triggers a spurious summary edit."""
        was_away = self._operator_away.pop(engagement_id, False)
        self._away_refusals.pop(engagement_id, None)
        # F3: a fresh away-episode starts with no cooldown so its first backstop
        # fires immediately.
        self._away_force_cooldown_until.pop(engagement_id, None)
        # A2b: reset the once-per-episode force-end guard so a fresh away-episode
        # can fire again. The epoch mark self-clears when _log_abnormal_exit
        # consumes it, so it is NOT dropped here (the respawn's spawn event may
        # arrive after this clear).
        self._away_suspend_fired.discard(engagement_id)
        # A2b (Sol A2 review): cancel an in-flight force-suspend kill — the
        # operator's own inbound is now the turn boundary, so a still-verifying
        # SIGTERM/SIGKILL ladder against a possibly-already-respawned generation
        # is both moot and racy. force_turn_boundary tolerates cancellation at
        # any await (nothing half-signalled). Whole-branch gate r3: cancel IN
        # PLACE (no pop) — the done callback retires the handle, keeping the
        # owner visible to ``drain_force_cleanups`` until its shielded cleanup
        # has been handed off or finished.
        force_task = self._force_tasks.get(engagement_id)
        if force_task is not None and not force_task.done():
            force_task.cancel()
        if not was_away:
            return
        try:
            await self.recompute_engagement_status(engagement_id)
        except Exception:  # noqa: BLE001 — status recompute is advisory
            logger.debug(
                "recompute after operator-away clear failed", exc_info=True)

    async def reconcile_open_questions(
        self, engagement: EngagementRecord, snapshot: list[dict] | None = None,
    ) -> None:
        """§4 boot reconciliation: settle the ATTACH-TIME snapshot of open
        questions. Each stale keyboard/anchor is edited to the expired copy with
        the keyboard cleared, then removed from the ledger;
        ``next_question_number`` is NEVER rewound. Tested for button, free-text,
        and the commit-then-kill window.

        Review I1: the entries reconciled are the attach-time snapshot, which is
        by definition PRIOR-PROCESS (the in-memory broker starts empty at boot,
        so no snapshot entry can have a live broker record). We therefore settle
        them UNCONDITIONALLY — no blanket "any live ask ⇒ skip all" gate, which
        would let a fresh same-process ask (registered concurrently with this
        task) suppress settling genuinely-stale prior-process keyboards. A fresh
        ask allocates a NEW question number and a NEW ledger entry not present in
        the snapshot, so it is never touched here. Callers that omit ``snapshot``
        (legacy / direct invocation) fall back to a fresh read of the record."""
        rec = engagement
        open_qs = (
            list(snapshot) if snapshot is not None
            else list(getattr(rec, "open_questions", ()) or ())
        )
        if not open_qs:
            return

        # §A3 (Sol r5-3): settle EVERY tracked copy (current + each ``stale_mids``
        # copy) behind the confirmed-edit gate, removing the entry only when the
        # current copy is confirmed AND no stale copy remains. The reconcile copy
        # is chosen by the ``answered`` flag (∪ the live overlay): an answered
        # entry reads ``✅ answered below`` — R1 recovery unchanged, an ordinary
        # prior-process open question reads ``⌛ expired``.
        #
        # DEADLOCK AUDIT (B2, Sol r5-3): take the engagement's maintenance lock
        # around the per-entry work and RE-READ each entry FRESH by number in-lock
        # — the pre-service SNAPSHOT only determines WHICH questions are
        # prior-process; their CURRENT mids/state come from the in-lock re-read, so
        # a settle/re-anchor concurrent with boot serializes here instead of racing
        # a captured snapshot. Lock order maintenance → registry; the settle's wire
        # edits use the RAW edit primitive (never the sequencer lock) — no cycle.
        # This task runs at boot with no sequencer lock held.
        async with self.ask_maintenance_lock(rec.id):
            for q in open_qs:
                n = q.get("n")
                fresh = (self._reread_open_question(rec.id, n)
                         if n is not None else None)
                if fresh is None and n is not None:
                    # B3 (wave 2): a NUMBERED snapshot entry ABSENT on the in-lock
                    # fresh re-read was ALREADY resolved (settled + removed) between
                    # snapshot capture and this readiness-gated pass. SKIP — never
                    # fall back to the stale snapshot, whose ``answered=False`` would
                    # ⌛-overwrite a ✅ settle on a message that is already done.
                    logger.debug(
                        "boot reconcile: Q%s absent on fresh re-read (already "
                        "resolved) — skipping (eng=%s)", n, rec.id[:8])
                    continue
                # A legacy no-number snapshot entry (``n is None``) cannot be
                # re-read; settle it from the snapshot as before.
                entry = fresh if fresh is not None else q
                answered = bool(entry.get("answered", False)) or \
                    self._overlay_answered(rec.id, n)
                await self._settle_ledger_entry(
                    rec, entry, answered_suffix=answered)

        # F1 (Sol diff gate): entries were closed above without touching the
        # pinned summary — refresh it so its open-questions line reflects the
        # post-reconcile set (the open-question accessor reads live, so refresh
        # picks up exactly the entries that closed).
        ctrl = self._summaries.get(rec.id)
        if ctrl is not None:
            try:
                await ctrl.refresh()
            except Exception:  # noqa: BLE001 — summary refresh is advisory
                logger.debug(
                    "summary refresh after open-question reconcile failed",
                    exc_info=True)

    def _select_oldest_anchor_entry(self, engagement_id: str) -> dict | None:
        """The oldest free-text anchor from the RAW ledger (answered or not) —
        the answer-lifecycle may already have flagged it answered (invisible to
        ``oldest_open_anchor``), and visual settlement iterates raw entries."""
        reg = self._registry
        if reg is None:
            return None
        entries_getter = getattr(reg, "open_question_entries", None)
        if entries_getter is not None:
            anchors = [
                q for q in entries_getter(engagement_id)
                if q.get("kind") == "anchor"
            ]
            if anchors:
                return min(anchors, key=lambda q: q.get("n", 0))
            return None
        getter = getattr(reg, "oldest_open_anchor", None)  # legacy registry
        return getter(engagement_id) if getter is not None else None

    def _reread_open_question(
        self, engagement_id: str, n: int | None,
    ) -> dict | None:
        """FRESH registry re-read of one open-question entry by number (§A3, B2).
        Returns the CURRENT entry (its live ``tg_message_id`` + ``stale_mids``) or
        ``None``. Callers use this INSIDE the maintenance lock so a settle never
        acts on a pre-lock snapshot that a concurrent re-anchor has superseded."""
        reg = self._registry
        if reg is None or n is None:
            return None
        getter = getattr(reg, "open_question_entries", None)
        if getter is None:
            return None
        for q in getter(engagement_id):
            if q.get("n") == n:
                return q
        return None

    async def _settle_anchor_entry_locked(
        self, engagement: EngagementRecord, n: int | None,
        *, fallback: dict | None = None,
    ) -> int | None:
        """Settle ONE anchor entry, re-read FRESH by number (§A3, B2). Assumes the
        caller either holds the maintenance lock (the ordinary answer path) OR is
        the relay poster running lock-free under the sequencer lock (the entry is
        already answered there, so a racing re-anchor's revalidate declines). A
        legacy no-number entry (``n is None``) settles the ``fallback`` dict."""
        entry = self._reread_open_question(engagement.id, n) if n is not None else None
        if entry is None:
            if n is not None:
                # B3 (wave 2): a NUMBERED entry ABSENT on the fresh re-read was
                # ALREADY resolved (settled + removed) — SKIP, never re-edit from
                # the captured ``fallback`` snapshot (its ``answered=False`` would
                # ⌛-overwrite a ✅ settle). The ``fallback`` is ONLY for a legacy
                # no-number entry (``n is None``), which cannot be re-read.
                logger.debug(
                    "settle: anchor Q%s absent on fresh re-read (already "
                    "resolved) — skipping (eng=%s)", n, engagement.id[:8])
                return None
            entry = fallback
        if entry is None:
            return None
        return await self._settle_ledger_entry(
            engagement, entry, answered_suffix=True)

    async def _settle_open_anchor(
        self, engagement: EngagementRecord, operator_msg_id: int | None = None,
        *, anchor: dict | None = None,
    ) -> int | None:
        """§4/§A3: settle a free-text anchor — edit ``✅ answered below`` over it,
        remove its ledger entry (per the §A3 entry-removal invariant — only when
        the current settle is confirmed AND no ``stale_mids`` copy remains), and
        return the anchor's tg_message_id so the turn threads to the QUESTION it
        answers. Returns ``None`` when no anchor is open.

        The enqueue-time PROMOTION and the delivery-time settle share this one
        implementation: promotion passes the explicit ``anchor`` (only to pick the
        question NUMBER); the delivery-time path passes none and the oldest raw
        anchor number is selected. Every staged stale copy is settled behind the
        same confirmed-gate.

        DEADLOCK AUDIT (B2, Sol interleaving): this acquires the per-engagement
        ``ask_maintenance_lock`` and RE-READS the entry FRESH by number INSIDE the
        lock — never a pre-lock snapshot — so an answer settlement can no longer
        edit a stale OLD copy and close the entry while a concurrent re-anchor
        persisted a NEW current copy. Lock order is maintenance → registry (the
        SAME order re-anchor uses); the settle's wire edits use the RAW edit
        primitive, never the sequencer lock, so no maintenance→sequencer→
        maintenance cycle can form. MUST NOT be called while holding the sequencer
        lock — the relay poster's ``settle_answered_anchor`` path uses the
        lock-free ``_settle_anchor_entry_locked`` helper for exactly that reason."""
        n = None
        if anchor is not None:
            n = anchor.get("n")
        else:
            sel = self._select_oldest_anchor_entry(engagement.id)
            if sel is None:
                return None
            n = sel.get("n")
            anchor = sel
        async with self.ask_maintenance_lock(engagement.id):
            amid = await self._settle_anchor_entry_locked(
                engagement, n, fallback=anchor)
        # W-R2: recompute the summary status from the remaining open questions
        # (still ⏳ waiting while another question is open; ⚙️ working once none
        # remain and the turn is running). Done OUTSIDE the maintenance lock.
        try:
            await self.recompute_engagement_status(engagement.id)
        except Exception:  # noqa: BLE001 — status recompute is advisory
            logger.debug("recompute after anchor settle failed", exc_info=True)
        return amid

    # -- v0.83.0 (§A3(b)) turn-end re-anchor: staged flow + latch + retry owner -
    def set_reanchor_due(self, engagement_id: str) -> None:
        """SET the re-anchor-due latch (idempotent). The standing OBLIGATION to
        keep the oldest unanswered anchor LAST, consumed at every turn boundary
        (§A3(b), Sol r10-1/r11-1). Set by the reservation-rollback consumer and
        by any boundary pass that fails/aborts."""
        self._reanchor_due.add(engagement_id)

    async def _consume_reanchor(self, engagement: EngagementRecord) -> None:
        """Run the re-anchor pass at a turn boundary and reconcile the latch +
        retry owner (§A3(b), Sol r13-1). A True pass (obligation met — nothing
        owed, already-last, or a successful staged re-anchor) CLEARS the latch and
        RETIRES the retry owner; a False pass (persist failure, post/wire failure)
        SETS the latch and ARMS the retry owner. The pass is idempotent and fully
        revalidated, so a concurrent boundary consumer racing it is harmless —
        one of them finds the obligation already discharged."""
        eng_id = engagement.id
        ok = await self._reanchor_pass(engagement)
        if ok:
            self._reanchor_due.discard(eng_id)
            self._retire_reanchor_retry(eng_id)
        else:
            self._reanchor_due.add(eng_id)
            self._arm_reanchor_retry(engagement)

    async def _reanchor_pass(self, engagement: EngagementRecord) -> bool:
        """§A3(b) staged turn-end re-anchor — ANCHORS ONLY. Keep the oldest
        UNANSWERED anchor the LAST item in the topic. Returns ``True`` when the
        latch MAY clear (nothing owed / obligation met), ``False`` when a retry is
        owed. Runs under the per-engagement ask-maintenance lock (shared with
        ``_settle_open_anchor`` and the ingress reservation; NEVER acquired while
        holding the sequencer lock)."""
        eng_id = engagement.id
        async with self.ask_maintenance_lock(eng_id):
            return await self._reanchor_pass_locked(engagement)

    async def _reanchor_pass_locked(self, engagement: EngagementRecord) -> bool:
        eng_id = engagement.id
        reg = self._registry
        # Select the oldest UNANSWERED, unreserved anchor (effective view: not
        # answered ∪ overlay, not reserved). No such anchor ⇒ nothing owed.
        anchor = self._oldest_unanswered_anchor(eng_id, exclude_reserved=True)
        if anchor is None:
            return True
        n = anchor.get("n")
        old_mid = anchor.get("tg_message_id")
        seq = self._sequencers.get(eng_id)
        if seq is None:
            # No sequencer to measure high-water / post through — cannot
            # re-anchor. Treat as nothing we can do (degraded); the boot
            # reconciler is the eventual backstop.
            return True
        hw = seq.high_water
        # Already LAST (nothing posted below it this turn) ⇒ nothing owed. A
        # ``None`` high-water means the sequencer recorded no post at/after the
        # anchor, so it is trivially last too.
        if old_mid is not None and (hw is None or old_mid >= hw):
            return True

        body = anchor.get("text") or (f"Q{n}:" if n is not None else "")

        # Step 1 — STAGE (strict): stale_mids += [old_mid]. A raise aborts with
        # NOTHING on the wire (return False — retry owed).
        if old_mid is not None and n is not None:
            stage = getattr(reg, "stage_stale_mid", None) if reg is not None else None
            if stage is not None:
                try:
                    await stage(eng_id, n, old_mid)
                except Exception:  # noqa: BLE001 — strict stage failed
                    logger.warning(
                        "engagement %s: re-anchor stage_stale_mid failed (n=%s) "
                        "— aborting, nothing on wire", eng_id[:8], n,
                        exc_info=True)
                    return False

        # Step 2 — POST the new anchor copy through the sequencer with a
        # ``revalidate`` hook that re-checks (under the sequencer lock,
        # immediately before the send) that the anchor is STILL the oldest
        # unanswered+unreserved one (Sol r8-2): an answer that landed during the
        # awaited step-1 stage DECLINES the send.
        declined = False

        def _revalidate() -> bool:
            nonlocal declined
            cur = self._oldest_unanswered_anchor(eng_id, exclude_reserved=True)
            still = cur is not None and cur.get("n") == n
            if not still:
                declined = True
            return still

        new_mid = await seq.post_discrete(body, revalidate=_revalidate)
        if new_mid is None:
            # Un-stage best-effort (a failed un-stage leaves an overlap-tolerant
            # stale_mid — reconciliation tolerates the overlap).
            await self._best_effort_unstage(eng_id, n, old_mid)
            if declined:
                # The answer won — nothing owed.
                return True
            # Wire failure — retry owed.
            return False

        # Step 3 — STRICT-PERSIST tg_message_id = new_mid. A raise settles the
        # NEW copy to '↪ see the question above' (confirmed-gate, best-effort,
        # plain text) and aborts; the original stays live and tracked.
        update = (getattr(reg, "update_question_mid", None)
                  if reg is not None else None)
        if update is not None and n is not None:
            try:
                await update(eng_id, n, new_mid)
            except Exception:  # noqa: BLE001 — strict persist failed
                logger.warning(
                    "engagement %s: re-anchor update_question_mid failed (n=%s) "
                    "— settling the new copy 'see above'", eng_id[:8], n,
                    exc_info=True)
                await self._confirm_settle_mid(
                    engagement, new_mid, f"{body}\n{_OPEN_Q_SEE_ABOVE}", n)
                return False

        # Step 4 — SETTLE-EDIT the OLD copy to '↪ question re-posted below ↓'
        # (confirmed-gate). ONLY on a confirmed edit un-stage old_mid; an
        # unconfirmed edit retains the stale_mid for boot reconciliation — the
        # OBLIGATION is already met (the question is now LAST at new_mid).
        if old_mid is not None:
            old_confirmed = await self._confirm_settle_mid(
                engagement, old_mid, f"{body}\n{_OPEN_Q_REPOSTED_BELOW}", n)
            if old_confirmed and n is not None:
                await self._best_effort_unstage(eng_id, n, old_mid)
        return True

    async def _best_effort_unstage(
        self, engagement_id: str, n: int | None, mid: int | None,
    ) -> None:
        """Best-effort strict un-stage of a stale mid (§A3(b) overlap-tolerance):
        a failure leaves a stale_mid pointing at a message that may ALSO still be
        the live ``tg_message_id`` — reconciliation tolerates the overlap."""
        reg = self._registry
        if reg is None or n is None or mid is None:
            return
        unstage = getattr(reg, "unstage_stale_mid", None)
        if unstage is None:
            return
        try:
            await unstage(engagement_id, n, mid)
        except Exception:  # noqa: BLE001 — overlap-tolerant
            logger.debug(
                "engagement %s: re-anchor un-stage failed (n=%s mid=%s)",
                engagement_id[:8], n, mid, exc_info=True)

    def _arm_reanchor_retry(self, engagement: EngagementRecord) -> None:
        """Arm the ONE retry-owner task for this engagement (§A3(b), Sol r13-1).
        A double-arm while a task already runs is a NO-OP. The task self-retires
        (removing its dict entry) via a done-callback."""
        eng_id = engagement.id
        existing = self._reanchor_retry_tasks.get(eng_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._reanchor_retry_loop(engagement),
            name=f"reanchor_retry:{eng_id[:8]}")
        self._reanchor_retry_tasks[eng_id] = task
        task.add_done_callback(
            lambda t, eid=eng_id: self._on_reanchor_retry_done(eid, t))

    def _on_reanchor_retry_done(
        self, engagement_id: str, task: asyncio.Task,
    ) -> None:
        # Remove the dict entry only if it still points at THIS task (a fresh
        # arm may have replaced it). A completed task leaves no reference.
        if self._reanchor_retry_tasks.get(engagement_id) is task:
            self._reanchor_retry_tasks.pop(engagement_id, None)

    def _retire_reanchor_retry(self, engagement_id: str) -> None:
        """Cancel + drop the retry owner (a boundary consumer / promotion /
        terminal settle discharged the obligation)."""
        task = self._reanchor_retry_tasks.pop(engagement_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _reanchor_retry_loop(self, engagement: EngagementRecord) -> None:
        """Bounded-backoff self-rescheduling retry owner (§A3(b), Sol r13-1):
        loop until a True pass clears the latch (success / the question got
        answered elsewhere). ``CancelledError`` terminates WITHOUT rescheduling
        (engagement teardown / a boundary consumer beat it). The pass is
        idempotent + revalidated, so overlap with a boundary consumer is
        harmless."""
        eng_id = engagement.id
        idx = 0
        try:
            while eng_id in self._reanchor_due:
                delay = _REANCHOR_BACKOFF[min(idx, len(_REANCHOR_BACKOFF) - 1)]
                idx += 1
                await self._reanchor_retry_sleep(delay)
                if eng_id not in self._reanchor_due:
                    break
                ok = await self._reanchor_pass(engagement)
                if ok:
                    self._reanchor_due.discard(eng_id)
                    break
        except asyncio.CancelledError:
            raise

    async def settle_all_open_questions(
        self, engagement: EngagementRecord, outcome: str,
    ) -> None:
        """§A3(b) consumer (c) — terminal engagement finalize SETTLES every
        remaining open-question entry (raw view) instead of re-anchoring, closing
        the latent gap where ``/cancel``/``/complete`` left a live free-text
        anchor visually open forever. Answered entries get the ✅ copy; unanswered
        ones an outcome-appropriate copy (🛑 ended for cancelled/error, ⌛ expired
        otherwise). The entry-removal invariant is honored. Clears the latch +
        cancels the retry owner (the terminal settle IS the obligation's
        discharge). Best-effort per entry — never raises into the finalize funnel.
        Called getattr-tolerantly by ``tools._finalize_engagement``."""
        eng_id = engagement.id
        # Discharge the re-anchor obligation: nothing to keep last once terminal.
        self._reanchor_due.discard(eng_id)
        self._retire_reanchor_retry(eng_id)
        reg = self._registry
        entries_getter = (getattr(reg, "open_question_entries", None)
                          if reg is not None else None)
        if entries_getter is None:
            return
        cancelled = outcome in ("cancelled", "error", "failed")
        async with self.ask_maintenance_lock(eng_id):
            for q in list(entries_getter(eng_id)):
                answered = bool(q.get("answered", False)) or self._overlay_answered(
                    eng_id, q.get("n"))
                try:
                    if answered:
                        await self._settle_ledger_entry(
                            engagement, q, answered_suffix=True)
                    else:
                        await self._settle_ledger_entry(
                            engagement, q, answered_suffix=False,
                            override_suffix=(
                                _OPEN_Q_CANCELLED_SUFFIX if cancelled
                                else _OPEN_Q_EXPIRED_SUFFIX),
                        )
                except Exception:  # noqa: BLE001 — never abort finalize
                    logger.warning(
                        "engagement %s: terminal open-question settle failed "
                        "(n=%s)", eng_id[:8], q.get("n"), exc_info=True)

    def set_engagement_reply_anchor(
        self, engagement_id: str, message_id: int,
    ) -> None:
        """§4 causal handoff: a button answer continues the SAME CLI turn — the
        telegram commit helper sets this one-shot anchor so the turn's FIRST
        sequencer output threads its reply to the ask message. SYNCHRONOUS (no
        await) — the caller relies on the pre-resumption guarantee."""
        seq = self._sequencers.get(engagement_id)
        if seq is not None:
            seq.set_turn_reply_to(message_id)

    async def edit_ask_keyboard(
        self, engagement_id: str, message_id: int, markup: Any,
        *, revalidate: Any = None,
    ) -> bool:
        """A5 · F-MULTI: markup-only redraw of a live multi-select keyboard
        through the sequencer's ``edit_discrete`` (§A9) so it serializes on the
        same single-writer lock as the settle edit. ``revalidate`` (the toggle
        terminal-race guard) runs under the lock immediately before the wire
        edit. Returns False (no edit) when the engagement has no live sequencer."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return False
        return await seq.edit_discrete(
            message_id, markup=markup, revalidate=revalidate)

    async def settle_ask_keyboard(
        self, engagement_id: str, message_id: int, text: str,
    ) -> bool:
        """A5 · F-MULTI: the multi ask's terminal settle edit — set the settle
        text AND clear the keyboard, routed through the SAME ``edit_discrete``
        primitive as the toggle redraw so a stale redraw can never land after
        (and resurrect) a settled keyboard. Returns False when the engagement
        has no live sequencer (the finish hook then leaves the ledger intact for
        boot reconciliation)."""
        seq = self._sequencers.get(engagement_id)
        if seq is None:
            return False
        from channels.output_sequencer import MARKUP_EMPTY
        return await seq.edit_discrete(
            message_id, text=text, markup=MARKUP_EMPTY)

    async def _supersede_pending_asks(self, engagement_id: str) -> None:
        """§4 live-ask supersession: a fresh operator message resolves any
        PENDING engagement_ask keyboard as ``superseded_by_text`` (broker cancel
        path) so it settles immediately instead of dead-waiting its timeout. The
        keyboard's finish hook renders the superseded copy + clears the buttons.
        Free-text anchors register no broker request, so they are untouched."""
        from verdict_broker import BROKER
        BROKER.cancel_scope(
            namespace="engagement_ask", scope=engagement_id,
            reason="superseded_by_text",
        )

    async def _run_topic_relay(self, engagement: EngagementRecord) -> None:
        """Drive the always-on live topic-stream relay for one engagement.

        The relay reads the engagement's NDJSON s6-log to the live end then
        returns; each claude_code turn is a fresh CLI spawn that appends a
        burst then exits, so we re-run on a short poll — the crash-safe cursor
        (``<ws>/.stream_cursor.json``) resumes exactly where the last run left
        off, and REPLAY-mode side-effect suppression keeps re-runs idempotent.
        """
        from drivers.topic_stream import TopicStreamRelay

        ws = Path(self._engagements_root) / engagement.id
        relay = TopicStreamRelay(
            engagement_id=engagement.id,
            topic_id=engagement.topic_id,
            log_dir=engagement_log_dir(engagement.id),
            cursor_path=str(ws / ".stream_cursor.json"),
            send_message=self._relay_send_message,
            edit_message=self._relay_edit_message,
            delete_message=self._relay_delete_message,
            on_turn_event=(
                lambda kind, payload: self._on_stream_event(
                    engagement, kind, payload)
            ),
            reply_texts=lambda: self._reply_texts.get(engagement.id, set()),
            # v0.79.0 (§2): the SHARED per-engagement sequencer, so the relay
            # and the discrete ingresses agree on ordering + high-water.
            sequencer=self._ensure_sequencer(engagement),
        )
        while True:
            try:
                await relay.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — relay is best-effort
                logger.warning(
                    "topic relay for engagement %s errored (will retry): %s",
                    engagement.id[:8], exc,
                )
            await asyncio.sleep(0.5)

    # -- relay-injected Telegram primitives -------------------------------

    async def _relay_send_message(
        self, topic_id: int, text: str, reply_to: int | None = None,
    ) -> int | None:
        # v0.79.0 (§3): the sequencer threads the turn's first post to the
        # inbound operator message (reply-quoting). Only pass the kwarg when a
        # target exists so the common no-thread path stays 2-arg.
        if reply_to is not None:
            return await self._send_to_topic(
                topic_id, text, reply_to_message_id=reply_to)
        return await self._send_to_topic(topic_id, text)

    async def _relay_edit_message(
        self, topic_id: int, message_id: int, text: str,
    ) -> bool:
        if self._edit_topic_message is None:
            return False
        return await self._edit_topic_message(topic_id, message_id, text)

    async def _relay_delete_message(
        self, topic_id: int, message_id: int,
    ) -> bool:
        if self._delete_topic_message is None:
            return False
        return await self._delete_topic_message(topic_id, message_id)

    async def _relay_send_message_markup(
        self, topic_id: int, text: str, markup, reply_to: int | None = None,
    ) -> int | None:
        # A9: markup-capable discrete send (post_discrete). Returns None when the
        # primitive is un-injected so the sequencer records a failed post.
        if self._send_topic_message_markup is None:
            return None
        return await self._send_topic_message_markup(
            topic_id, text, markup, reply_to=reply_to)

    async def _relay_edit_message_markup(
        self, topic_id: int, message_id: int, text, markup,
    ) -> bool:
        # A9: markup-capable discrete edit (edit_discrete).
        if self._edit_topic_message_markup is None:
            return False
        return await self._edit_topic_message_markup(
            topic_id, message_id, text, markup)

    # -- stream-event fan-out ---------------------------------------------

    def record_reply_text(self, engagement_id: str, text: str) -> None:
        """Record a ``reply()`` text for the relay's per-turn de-dup.

        Called by the /internal/channel/send_to_topic handler (the engagement
        reply path). The set is cleared on each ``turn_start`` (see
        ``_on_stream_event``)."""
        if text:
            self._reply_texts.setdefault(engagement_id, set()).add(text)

    async def _on_stream_event(
        self, engagement: EngagementRecord, kind: str, payload: dict,
    ) -> None:
        """Fan the relay's ordered ``on_turn_event`` kinds into driver state.

        ``spawn`` → arm the inbound spool (redeliver survivors) + epoch/
        abnormal-exit correlation; ``turn_start`` → reset the reply-text de-dup
        set + mark the turn running + consume the delivered envelope (§3
        turn_start evidence) + retry receipts; ``mutating_tool``
        → W2/Sol B9 (Task 7): while the engagement's ``interaction_state``
        is ``awaiting_operator``, post ONE in-topic violation notice (per
        engagement) and flag ``rec.origin["interaction_violated"]`` via
        ``registry.set_interaction_violated`` — ``_finalize_engagement``
        surfaces it in the completion summary. A ``reply``/``ask``/
        ``set_progress`` control tool-use never reaches here —
        ``topic_stream.is_mutating_tooluse`` excludes them. ``result`` →
        clear the epoch pending a result (normal turn boundary)."""
        eng_id = engagement.id
        if kind == "spawn":
            epoch = payload.get("epoch")
            prev = self._epoch_pending.get(eng_id)
            if prev is not None:
                # A previous epoch spawned but never emitted a result before
                # this new spawn — abnormal exit. ``result`` is at-most-once,
                # so a spawn-without-result is an equally valid turn boundary.
                self._log_abnormal_exit(engagement, prev)
            # A new spawn means the prior turn ended (result) or died
            # (abnormal) — no turn is running until turn_start.
            self._turn_running[eng_id] = False
            self._epoch_pending[eng_id] = epoch
            spool = self._inbound.get(eng_id)
            if spool is not None:
                await spool.on_spawn()
            if prev is not None:
                # §A3(b) consumer (b): spawn-without-result — the prior turn died
                # abnormally (an equally valid turn boundary). Consume the
                # re-anchor latch AFTER on_spawn so a redelivered survivor never
                # lands below the re-anchored question.
                await self._consume_reanchor(engagement)
        elif kind == "turn_start":
            # Fresh turn — drop the prior turn's reply-text de-dup set, mark the
            # turn running (receipts are now due for new inbound), and consume
            # the envelope this turn carried (§3 turn_start evidence).
            self._reply_texts[eng_id] = set()
            self._turn_running[eng_id] = True
            # §4: a fresh turn resets the consecutive-ask-refusal escalation.
            self._ask_refusals[eng_id] = 0
            spool = self._inbound.get(eng_id)
            if spool is not None:
                await spool.on_turn_start()
            # §5: a running turn ⇒ ⚙️ working. Reset the elapsed base + start the
            # tick FIRST (so the working-status flush already reflects it).
            summary = self._summaries.get(eng_id)
            if summary is not None:
                from drivers.summary_controller import STATUS_WORKING
                await summary.note_turn_start()
                await self._summary_status_transition(eng_id, STATUS_WORKING)
        elif kind == "mutating_tool":
            logger.debug(
                "engagement %s mutating tool during turn: %s",
                eng_id[:8], payload.get("tool"),
            )
            reg = self._registry
            rec = reg.get(eng_id) if reg is not None else None
            if (rec is not None
                    and getattr(rec, "interaction_state", "") == "awaiting_operator"):
                # B4 (Sol r1): each effect is attempted and marked INDEPENDENTLY.
                # A failure is swallowed (logged) so the relay keeps consuming
                # and the NEXT mutating_tool frame retries the un-marked effect
                # — the marker is set only after success, so at most one
                # SUCCESSFUL notice + one flag-persist ever fire per engagement.
                if eng_id not in self._violation_notified:
                    try:
                        # F2 (Sol r2): the violation notice is a PLATFORM notice
                        # and MUST go through the single writer — a direct
                        # send_to_topic posts it AROUND the sequencer, landing
                        # below open narration while ``edit_narration_if_latest``
                        # still returns APPLIED (the exact §2 ordering violation).
                        # Route through ``post_platform_notice`` (seals narration
                        # + advances high-water under the one lock); direct send
                        # only when no live sequencer.
                        notice = (
                            "The agent took an action while waiting for your "
                            "reply — flagging this engagement for review."
                        )
                        seq = self._sequencers.get(eng_id)
                        if seq is not None:
                            posted = await seq.post_platform_notice(notice)
                            if posted is None:
                                raise RuntimeError("notice post returned no id")
                        else:
                            await self._send_to_topic(engagement.topic_id, notice)
                        self._violation_notified.add(eng_id)
                    except Exception:  # noqa: BLE001 — retry on the next frame
                        logger.warning(
                            "engagement %s: violation notice post failed; "
                            "will retry on next mutating tool",
                            eng_id[:8], exc_info=True,
                        )
                if eng_id not in self._violation_flagged:
                    set_violated = getattr(reg, "set_interaction_violated", None)
                    if set_violated is not None:
                        try:
                            await set_violated(eng_id)
                            self._violation_flagged.add(eng_id)
                        except Exception:  # noqa: BLE001 — retry on the next frame
                            logger.warning(
                                "engagement %s: set_interaction_violated failed; "
                                "will retry on next mutating tool",
                                eng_id[:8], exc_info=True,
                            )
        elif kind == "tool_use":
            # §5: EVERY tool_use block drives the summary's activity + plan
            # progress (NEVER status). Skips the engagement-channel CONTROL
            # tools (ask/reply/set_progress) — they are lifecycle-status
            # signals, not work activity.
            summary = self._summaries.get(eng_id)
            if summary is not None:
                name = payload.get("tool") or ""
                if not name.startswith("mcp__casa-engagement-channel__"):
                    from drivers.summary_controller import (
                        activity_for_tool, extract_plan,
                    )
                    await summary.submit_activity(activity_for_tool(name))
                    plan = extract_plan(name, payload.get("input") or {})
                    if plan is not None:
                        await summary.submit_plan(**plan)
        elif kind == "result":
            self._epoch_pending[eng_id] = None
            self._turn_running[eng_id] = False
            spool = self._inbound.get(eng_id)
            if spool is not None:
                await spool.on_turn_end()
            # §A3(b) consumer (a) — the ordinary turn-end re-anchor pass, pinned
            # to run AFTER ``spool.on_turn_end()`` (Sol r13-2): a notice the spool
            # flushes at turn end must land BEFORE a just-re-anchored question, so
            # the re-anchored anchor stays LAST.
            await self._consume_reanchor(engagement)
            # §5: a finished turn ⇒ the ball is with the operator (⏳ waiting for
            # your reply). Stop the elapsed tick + mandatory turn-end flush.
            summary = self._summaries.get(eng_id)
            if summary is not None:
                from drivers.summary_controller import STATUS_WAITING_REPLY
                await self._summary_status_transition(
                    eng_id, STATUS_WAITING_REPLY)
                await summary.note_turn_end()

    def _log_abnormal_exit(
        self, engagement: EngagementRecord, epoch: int | None,
    ) -> None:
        short = engagement.id[:8]
        # A2b: an epoch we force-ended as the operator-away backstop is NOT a
        # scary abnormal exit — log the expected forced suspend at INFO and
        # consume the mark (a later, genuinely-abnormal exit still WARNs).
        if (epoch is not None
                and self._forced_suspend_epochs.get(engagement.id) == epoch):
            self._forced_suspend_epochs.pop(engagement.id, None)
            logger.info(
                "engagement %s: epoch %s ended by forced suspend "
                "(operator away)", short, epoch,
            )
            return
        tail = self._read_epoch_stderr_tail(engagement, epoch)
        if tail is None:
            logger.warning(
                "engagement %s: epoch %s exited without a result frame "
                "(abnormal); stderr diagnostics unavailable", short, epoch,
            )
        else:
            logger.warning(
                "engagement %s: epoch %s exited without a result frame "
                "(abnormal); stderr tail:\n%s", short, epoch, tail,
            )

    def _read_epoch_stderr_tail(
        self, engagement: EngagementRecord, epoch: int | None,
        *, max_bytes: int = 4000,
    ) -> str | None:
        """Read the UNIQUE per-epoch stderr ring (Sol r5-B2).

        The filename carries the epoch, so no sidecar / ownership check is
        needed — a lingering ringlog consumer only ever writes ITS OWN epoch's
        file. Reads ``.stderr.<epoch>.log.1`` (older rotated chunk) then
        ``.stderr.<epoch>.log`` (newest), returning the tail; both absent
        (never created / already pruned) → ``None`` (diagnostics unavailable,
        never misattributed to a reused slot)."""
        if epoch is None:
            return None
        ws = Path(self._engagements_root) / engagement.id
        chunks: list[str] = []
        for name in (f".stderr.{epoch}.log.1", f".stderr.{epoch}.log"):
            try:
                chunks.append(
                    (ws / name).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        if not chunks:
            return None
        return "".join(chunks)[-max_bytes:]

    async def _relay_log_lines(
        self, engagement: EngagementRecord, *, log_path: str,
    ) -> None:
        """Tail the per-engagement s6-log file and emit each line at DEBUG.

        Phase 4b G5 — companion to Bug 4's stderr callback. Stderr from the
        in_casa-driver path lands on the ``subprocess_cli`` logger via the SDK
        callback (sdk_logging.make_stderr_logger).

        v0.75.0/JC3: claude_code's CLI subprocess NO LONGER merges stderr into
        s6-log — the run script redirects stderr into a bounded per-epoch ring
        (``exec 2> >(ringlog.sh .stderr.<EPOCH>.log ...)``), so s6-log/current
        carries only the CLI's NDJSON stdout. This DEBUG relay simply mirrors
        that stdout stream (``_tail_file`` with ``from_end=True``, inode
        rotation handled) into the ``subprocess_cli`` logger, staying
        DEBUG-only: prod operators see nothing, a single LOG_LEVEL=DEBUG flip
        surfaces everything. It is INDEPENDENT of the always-on
        ``TopicStreamRelay`` (which parses the same NDJSON into the operator's
        live topic window and is spawned regardless of LOG_LEVEL). Per-epoch
        stderr is surfaced separately by the abnormal-exit correlation in
        ``_on_stream_event`` / ``_read_epoch_stderr_tail``.

        v0.64.0 removed the sibling ``_capture_url`` task: headless claude
        auto-degrades to one-shot --print mode on non-TTY stdout and never
        prints a remote-control URL line, so there is nothing to capture
        (live-verified; see the 2026-07-10 remote-control-honesty design).
        """
        short = engagement.id[:8]
        relay_logger = logging.getLogger("subprocess_cli")
        async for line in _tail_file(log_path, from_end=True):
            relay_logger.debug(
                "stdout %s", line.rstrip("\n"),
                extra={"engagement_id": short},
            )

    async def _capture_session_id(
        self, engagement: EngagementRecord, *,
        poll_interval_s: float = 0.5,
    ) -> None:
        """P31 (v0.37.10): watch the claude CLI's own session-storage
        directory for the first ``<uuid>.jsonl`` file. The filename
        (minus extension) IS the SDK session UUID. Persist to
        ``<workspace>/.session_id`` so a boot-replay's
        ``--resume $(cat .session_id)`` flag carries the conversation
        forward.

        Replaces v0.37.9's s6-log tailing approach, which was
        non-functional at the time: until v0.64.0 the s6-rc log pipeline
        was never compiled (nested log/ subdir — see
        ``s6_rc.write_service_dir``), so the log file did not exist.
        Watching the CLI's own session storage is retained even now that
        the log pipeline works: it observes the authoritative artifact
        directly. Bug-review:
        ``docs/bug-review-2026-05-14-exploration6.md::O-5``.

        Claude CLI session storage layout (HOME=<ws>/.home, CWD=<ws>):

            <ws>/.home/.claude/projects/-data-engagements-<id>/<uuid>.jsonl

        The directory-name encoding replaces ``/`` with ``-`` in the
        workspace path (claude CLI native behavior).

        One-shot: returns after the first UUID-named .jsonl is found.
        Re-spawns on s6 restart see the persisted file and resume
        cleanly — see ``engagement_run_template.sh``.

        Atomic write: temp-file + ``os.replace`` so a Casa crash
        mid-write cannot leave a half-truncated ``.session_id``.
        """
        short = engagement.id[:8]
        ws = Path(self._engagements_root) / engagement.id
        target = ws / ".session_id"
        tmp = ws / ".session_id.tmp"
        projects_dir = (
            ws / ".home" / ".claude" / "projects"
            / f"-data-engagements-{engagement.id}"
        )
        while True:
            sid = self._scan_projects_dir_for_sid(projects_dir)
            if sid is not None:
                try:
                    tmp.write_text(sid + "\n", encoding="utf-8")
                    os.replace(tmp, target)
                except OSError as exc:
                    logger.warning(
                        "engagement %s session_id persist failed: %s",
                        short, exc,
                    )
                    return
                logger.info(
                    "engagement %s captured sdk session_id %s",
                    short, sid[:8],
                )
                if self._persist_session_id is not None:
                    try:
                        await self._persist_session_id(engagement.id, sid)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "engagement %s persist_session_id callback "
                            "failed: %s", short, exc,
                        )
                return
            await asyncio.sleep(poll_interval_s)

    @staticmethod
    def _scan_projects_dir_for_sid(projects_dir: Path) -> str | None:
        """Return the oldest UUID-named .jsonl in projects_dir, or None.

        Sort by mtime ascending so the first session file (the one
        spawned by the initial CLI start) wins over any later ones the
        CLI might write on a resume retry.
        """
        try:
            if not projects_dir.is_dir():
                return None
            candidates: list[tuple[float, str]] = []
            for p in projects_dir.iterdir():
                if p.suffix != ".jsonl":
                    continue
                stem = p.stem
                if _UUID_REGEX.match(stem) is None:
                    continue
                try:
                    candidates.append((p.stat().st_mtime, stem))
                except OSError:
                    continue
            if not candidates:
                return None
            candidates.sort()
            return candidates[0][1]
        except OSError:
            return None

    async def _poll_respawns(
        self, engagement: EngagementRecord, *, interval_s: float = 5.0,
    ) -> None:
        """Emit subprocess_respawn bus events when s6-svstat shows a new PID."""
        last_pid: int | None = None
        while True:
            await asyncio.sleep(interval_s)
            pid = await s6_rc.service_pid(engagement_id=engagement.id)
            if pid is None:
                continue
            if last_pid is not None and pid != last_pid:
                await self._publish_bus_event({
                    "event": "subprocess_respawn",
                    "engagement_id": engagement.id,
                    "previous_pid": last_pid,
                    "new_pid": pid,
                    "ts": time.time(),
                })
            last_pid = pid

    async def _publish_bus_event(self, event: dict) -> None:
        """Overridable (tests inject). Default no-op at driver layer —
        casa_core wires a real bus sink in at construction time (see Phase E)."""
        logger.debug("bus event (no sink wired): %s", event)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _tail_file(log_path: str, *, from_end: bool = False):
    """Yield new lines from a file as they appear. Terminates on task cancel.

    Bug 11 (v0.14.6): tracks the file's inode so rotation is handled.
    s6-log rotates ``current`` at 1 MB by renaming it to ``@<timestamp>.s``
    and creating a fresh ``current``. Pre-fix the loop kept seeking to
    the OLD pos in the new (smaller) file, so all lines below the prior
    cutoff were silently dropped. Now: when ``st_ino`` changes, reset
    ``pos`` to 0 so the new file is read from its start. We also reset
    if the file shrinks below ``pos`` (truncate-in-place pattern).

    v0.64.0 (file is now real in production):
      - ``from_end=True`` starts at the file's current end when it already
        exists at first sight — boot replay re-attaches without re-yielding
        up to 1 MB of history. A file that appears later (fresh engagement)
        is still read from its start.
      - A transient OSError mid-cycle (rotation renames ``current`` between
        ``exists()`` and ``open()``) retries next tick instead of killing
        the (unobserved) consumer task.
    """
    path = Path(log_path)
    pos = 0
    last_inode: int | None = None
    first_sight = True
    while True:
        try:
            exists = path.exists()
            if first_sight:
                first_sight = False
                if exists and from_end:
                    try:
                        pos = path.stat().st_size
                    except OSError:
                        pos = 0
            if exists:
                try:
                    current_inode = path.stat().st_ino
                except OSError:
                    current_inode = None
                if last_inode is not None and current_inode != last_inode:
                    pos = 0
                last_inode = current_inode

                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(0, 2)            # SEEK_END
                    end = fh.tell()
                    if pos > end:
                        pos = 0
                    fh.seek(pos)
                    while True:
                        line = fh.readline()
                        if not line:
                            pos = fh.tell()
                            break
                        yield line
        except OSError:
            pass                             # transient — retry next tick
        await asyncio.sleep(0.1)
