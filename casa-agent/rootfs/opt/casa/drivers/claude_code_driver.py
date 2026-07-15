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
# restart orphans it (matches channel_handlers._SETTLE_EXPIRED verbatim).
_OPEN_Q_EXPIRED_SUFFIX = "\n⌛ expired — answer by text below"
# §4 free-text anchor: appended when the next operator message answers it.
_OPEN_Q_ANSWERED_SUFFIX = "\n✅ answered below"
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
        # §4: on delivery of an operator message, settle the oldest open
        # free-text anchor (✅ answered below) and thread the turn to it.
        # Returns the anchor's tg_message_id to thread to, or None.
        self._settle_anchor_on_delivery = settle_anchor_on_delivery
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
                # §4: an operator message settles the oldest open free-text
                # anchor (✅ answered below) and threads this turn to the ANCHOR
                # instead of the operator's own message.
                thread_to = env.tg_message_id
                if not env.is_initial and self._settle_anchor_on_delivery is not None:
                    try:
                        anchor_id = await self._settle_anchor_on_delivery(
                            env.tg_message_id)
                        if anchor_id is not None:
                            thread_to = anchor_id
                    except Exception:  # noqa: BLE001 — anchor settle is advisory
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
        pin_topic_message: Callable[[int, int], Awaitable[bool]] | None = None,
        registry: Any = None,
    ) -> None:
        self._engagements_root = engagements_root
        # ``send_to_topic`` doubles as the relay's ``send_message`` primitive:
        # casa_core wires it to return the posted Telegram message_id (the relay
        # needs it to edit the rolling message), while notice/warning callers
        # ignore the return.
        self._send_to_topic = send_to_topic
        self._edit_topic_message = edit_topic_message
        self._delete_topic_message = delete_topic_message
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
    ) -> None:
        spool = self._inbound.get(engagement.id)
        if spool is not None:
            await spool.enqueue(text, tg_message_id=tg_message_id)
        else:
            await self._write_to_fifo(engagement, text)

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
                        await self._send_to_topic(
                            engagement.topic_id,
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

    def _spawn_background_tasks(self, engagement: EngagementRecord) -> None:
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
        attach_open_qs = list(getattr(engagement, "open_questions", ()) or ())
        if attach_open_qs:
            tasks.append(asyncio.create_task(
                self.reconcile_open_questions(engagement, attach_open_qs)))

        self._tasks[engagement.id] = tasks

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
        """The summary's stable header — the engagement's topic-name string
        source (the concise task, minus the state emoji the status line owns)."""
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
                open_question_numbers=(
                    (lambda: reg.open_question_numbers(eid))
                    if reg is not None
                    and hasattr(reg, "open_question_numbers")
                    else (lambda: [])
                ),
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
        await ctrl.submit_status(status, rev)

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

        close = getattr(self._registry, "close_open_question", None)
        for q in open_qs:
            n = q.get("n")
            mid = q.get("tg_message_id")
            if mid is not None and self._edit_topic_message is not None:
                display = q.get("text") or (f"Q{n}:" if n is not None else "")
                text = f"{display}{_OPEN_Q_EXPIRED_SUFFIX}"
                try:
                    await self._edit_topic_message(
                        rec.topic_id, mid, text, clear_keyboard=True)
                except Exception:  # noqa: BLE001 — best-effort settle
                    logger.warning(
                        "engagement %s: open-question boot settle failed (n=%s)",
                        rec.id[:8], n, exc_info=True,
                    )
            if close is not None and n is not None:
                try:
                    await close(rec.id, n)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "engagement %s: close_open_question failed (n=%s)",
                        rec.id[:8], n, exc_info=True,
                    )

    async def _settle_open_anchor(
        self, engagement: EngagementRecord, operator_msg_id: int | None,
    ) -> int | None:
        """§4: settle the oldest open free-text anchor when an operator message
        is delivered — edit ``✅ answered below`` over the anchor, close its
        ledger entry, and return the anchor's tg_message_id so the turn threads
        to the QUESTION it answers. Returns ``None`` when no anchor is open."""
        reg = self._registry
        if reg is None:
            return None
        getter = getattr(reg, "oldest_open_anchor", None)
        if getter is None:
            return None
        anchor = getter(engagement.id)
        if anchor is None:
            return None
        n = anchor.get("n")
        amid = anchor.get("tg_message_id")
        display = anchor.get("text") or (f"Q{n}:" if n is not None else "")
        if amid is not None and self._edit_topic_message is not None:
            try:
                await self._edit_topic_message(
                    engagement.topic_id, amid,
                    f"{display}{_OPEN_Q_ANSWERED_SUFFIX}", clear_keyboard=True)
            except Exception:  # noqa: BLE001 — settle is advisory
                logger.debug("anchor settle edit failed", exc_info=True)
        close = getattr(reg, "close_open_question", None)
        if close is not None and n is not None:
            try:
                await close(engagement.id, n)
            except Exception:  # noqa: BLE001
                logger.debug("close_open_question (anchor) failed", exc_info=True)
        return amid

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
        tail = self._read_epoch_stderr_tail(engagement, epoch)
        short = engagement.id[:8]
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
