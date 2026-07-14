"""NDJSON engagement-log → Telegram topic relay (design §W1 — live topic streaming).

A ``claude_code`` engagement's CLI is invoked ``--print --verbose
--output-format stream-json`` (Task 4 run template), so its stdout is strictly
NDJSON captured by the s6-log producer/consumer pair. Each line is either a
Casa control frame (``{"casa_control": "spawn", "epoch": N}`` printed pre-exec)
or a CLI stream-json frame (``system``/``init``, ``assistant`` with content
blocks, ``result``, plus a tolerated ``rate_limit_event``). This module turns
that stream into a *live window* on the engagement: assistant TEXT accumulates
into ONE edited topic message per turn (rolling to a new message past Telegram's
limit), tool names/args/results and thinking are NEVER surfaced, and a
crash-safe SEGMENT-QUALIFIED cursor gives honest **at-least-once** visible
delivery (a documented rare duplicate — never exactly-once).

Design invariants pinned here (Sol adversarial review, §W1):

* **Checkpoint every fully-handled frame (Sol r3-B7)** — including invisible
  ones (spawn, ``system``/init, tool-only assistant, ``rate_limit_event``,
  unknown, textless result). Only a visible-text frame whose post/edit has not
  yet succeeded blocks cursor advancement. ``current`` therefore marks the
  boundary between already-applied side effects (``<= current``) and un-applied
  ones (``> current``).
* **Recovery with side-effect suppression (Sol r4-B5)** — recovery re-reads
  from ``turn_start``; frames ``<= current`` replay in REPLAY mode (rebuild the
  visible-text state ONLY; NO ``on_turn_event``, NO ``send_message``, NO failure
  counting), so a checkpointed in-turn ``spawn`` does not re-arm the inbound
  queue and a checkpointed ``mutating_tool`` does not re-flag. Past ``current``
  the relay goes LIVE.
* **Closed-turn checkpoint at ``result`` (Sol r5-B5)** — after the final edit
  and reply de-dup delete, persist ``message_ids=[]``,
  ``turn_start == current`` (past ``result``) so a restart-after-dedup never
  ghost-edits a deleted message.
* **Split a huge block (Sol r5-B4)** — a single 8-10K assistant text block
  becomes repeated ``<= _MSG_MAX`` sends in a loop, never one oversized message.
* **Drop mode (Sol r2-B5)** — after ``_DROP_THRESHOLD`` consecutive failures,
  one WARNING, keep consuming and advance ``dropped_through`` to the turn's
  terminal coordinate so a restart never replays the discarded tail.
* **Held-fd rotation drainage (Sol B6)** — when ``current``'s inode changes the
  held fd is drained to EOF before opening the successor; a missing start
  segment yields a retention-gap WARNING and resumes at ``current`` offset 0.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from atomic_io import atomic_write_json
from channels.output_sequencer import (
    APPLIED,
    FAILED,
    SEALED,
    OutputSequencer,
    projection_hash,
)

logger = logging.getLogger(__name__)

_MSG_MAX = 3900          # roll to a new topic message past this many chars
_DROP_THRESHOLD = 20     # consecutive Telegram failures before dropping the turn
_BACKOFF_BASE = 1.0      # seconds
_BACKOFF_MAX = 30.0      # seconds
_ZERO_SEG = (0, 0)

# Segment sentinel yielded by iter_log_segments when turn_start's segment is
# absent from disk (retention gap) — the relay logs a WARNING and resumes.
SEGMENT_GAP = "__gap__"

# Explicit non-mutating allowlist (Sol r3-B4). Everything else — including an
# unknown ``mcp__*`` tool — is treated as mutating. The engagement CONTROL
# tools are how the agent interacts while awaiting_operator, so they must NEVER
# flag a violation.
_NON_MUTATING_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "WebFetch",
        "WebSearch",
        "ToolSearch",
        "Skill",
        "mcp__casa-engagement-channel__reply",
        "mcp__casa-engagement-channel__ask",
        "mcp__casa-engagement-channel__set_progress",
    }
)


# ---------------------------------------------------------------------------
# Frame parsing / classification (pure functions).
# ---------------------------------------------------------------------------


def parse_frame(line: bytes) -> dict | None:
    """``json.loads`` one NDJSON line; return the object dict or ``None``.

    Non-JSON, non-object, or empty input → ``None`` (the caller skips and
    debug-logs). Never raises.
    """
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def extract_text_blocks(frame: dict) -> list[str]:
    """Assistant ``TextBlock`` text ONLY — never ``tool_use`` / ``thinking``.

    Mirrors ``agent.py::_make_on_message``'s TextBlock filter, but on raw
    stream-json dicts: ``{"type": "assistant", "message": {"content": [...]}}``.
    """
    if not isinstance(frame, dict) or frame.get("type") != "assistant":
        return []
    message = frame.get("message") or {}
    content = message.get("content") or []
    out: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                out.append(text)
    return out


def iter_content_blocks(frame: dict) -> list[tuple]:
    """Assistant content blocks IN ORDER (§2(3): per-block positions).

    Returns a list of ``("text", text)`` and ``("tool_use", name, input)``
    tuples in the block order the CLI emitted them; ``thinking`` blocks are
    dropped (never surfaced). One NDJSON assistant frame can interleave text
    and multiple tool_use blocks, so the relay must walk them in order to place
    a relay-mediated discrete post at exactly its block's position.
    """
    if not isinstance(frame, dict) or frame.get("type") != "assistant":
        return []
    message = frame.get("message") or {}
    out: list[tuple] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                out.append(("text", text))
        elif btype == "tool_use":
            out.append((
                "tool_use", block.get("name") or "", block.get("input") or {},
            ))
    return out


def is_mutating_tooluse(frame: dict) -> tuple[bool, str]:
    """``(True, tool_name)`` iff *frame* is an assistant ``tool_use`` for a
    mutating tool.

    Read-only tools (Read/Glob/Grep/WebFetch/WebSearch/ToolSearch/Skill) and the
    engagement control tools (``mcp__casa-engagement-channel__{reply,ask,
    set_progress}``) are non-mutating → ``(False, "")``. Anything else — every
    other tool, including unknown ``mcp__*`` — is mutating. The first mutating
    ``tool_use`` block found wins.
    """
    if not isinstance(frame, dict) or frame.get("type") != "assistant":
        return (False, "")
    message = frame.get("message") or {}
    for block in message.get("content") or []:
        if not (isinstance(block, dict) and block.get("type") == "tool_use"):
            continue
        name = block.get("name") or ""
        if name not in _NON_MUTATING_TOOLS:
            return (True, name)
    return (False, "")


# ---------------------------------------------------------------------------
# Segment-qualified crash-safe cursor.
# ---------------------------------------------------------------------------


def _zero_coord() -> dict:
    return {"segment": [0, 0], "offset": 0}


@dataclass
class StreamCursor:
    """Persisted at ``<ws>/.stream_cursor.json`` via temp+rename (atomic_io).

    Coordinates are SEGMENT-QUALIFIED: ``segment`` is a log file's stable
    ``[st_dev, st_ino]`` identity recorded at open, so an offset is never
    ambiguous across an s6-log rotation while Casa was down.
    """

    turn_start: dict = field(default_factory=_zero_coord)
    current: dict = field(default_factory=_zero_coord)
    message_ids: list[int] = field(default_factory=list)
    # Additive, backwards-tolerated (T1 review): per-message narration TEXT
    # length, parallel to ``message_ids`` — the boundaries at which the live
    # path split narration across messages (both _MSG_MAX and discrete-
    # interleave/seal rollovers). ``_replay_text`` splits reconstructed text at
    # these when present, falling back to _MSG_MAX-only when absent (legacy
    # checkpoints — the conservative recovery seal already covers them). The
    # design constraint pinned the cursor/checkpoint format UNCHANGED for the
    # RECOVERY-SEALING option; an absent-tolerated additive field honors that
    # intent (no incompatibility, replay converges).
    message_text_lens: list[int] = field(default_factory=list)
    last_posted_len: int = 0
    dropped_through: dict | None = None

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "StreamCursor":
        """Load the cursor; an absent/corrupt file yields an all-zero cursor."""
        try:
            with open(path, "rb") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(
            turn_start=data.get("turn_start") or _zero_coord(),
            current=data.get("current") or _zero_coord(),
            message_ids=list(data.get("message_ids") or []),
            message_text_lens=list(data.get("message_text_lens") or []),
            last_posted_len=int(data.get("last_posted_len") or 0),
            dropped_through=data.get("dropped_through"),
        )

    def save(self, path: str | os.PathLike[str]) -> None:
        atomic_write_json(
            path,
            {
                "turn_start": self.turn_start,
                "current": self.current,
                "message_ids": self.message_ids,
                "message_text_lens": self.message_text_lens,
                "last_posted_len": self.last_posted_len,
                "dropped_through": self.dropped_through,
            },
        )


# ---------------------------------------------------------------------------
# Held-fd segment generator (Sol B6).
# ---------------------------------------------------------------------------


def _seg_ident(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _ordered_segments(log_dir: str) -> list[tuple[str, tuple[int, int]]]:
    """``@*.s`` archives (chronological — TAI64N sorts lexically) then ``current``."""
    entries: list[tuple[str, tuple[int, int]]] = []
    try:
        names = sorted(os.listdir(log_dir))
    except OSError:
        names = []
    for name in names:
        if name.startswith("@") and name.endswith(".s"):
            path = os.path.join(log_dir, name)
            ident = _seg_ident(path)
            if ident is not None:
                entries.append((path, ident))
    cur = os.path.join(log_dir, "current")
    cur_ident = _seg_ident(cur)
    if cur_ident is not None:
        entries.append((cur, cur_ident))
    return entries


def _find_segment(log_dir: str, seg: tuple[int, int]) -> str | None:
    for path, ident in _ordered_segments(log_dir):
        if ident == seg:
            return path
    return None


def _successor_path(log_dir: str, seg: tuple[int, int]) -> str | None:
    """The path of the segment that follows *seg*, or ``None`` at the live end.

    Rebuilt fresh each call so a rotation that renamed the held ``current`` to
    an ``@*.s`` archive (and created a new ``current``) resolves correctly.
    """
    entries = _ordered_segments(log_dir)
    for i, (_path, ident) in enumerate(entries):
        if ident == seg:
            return entries[i + 1][0] if i + 1 < len(entries) else None
    # *seg* is no longer named on disk (fully drained held fd). If a different
    # ``current`` now exists, it is the successor; otherwise we are at the end.
    cur = os.path.join(log_dir, "current")
    cur_ident = _seg_ident(cur)
    if cur_ident is not None and cur_ident != seg:
        return cur
    return None


def _open_seg(path: str, offset: int):
    """``os.open`` + ``os.fdopen('rb')``; return ``(reader, (dev, ino))``."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    reader = os.fdopen(fd, "rb")
    if offset:
        reader.seek(offset)
    return reader, (st.st_dev, st.st_ino)


async def iter_log_segments(log_dir: str, start: dict):
    """Yield ``(segment, byte_offset_after, raw_line)`` from *start* onward.

    ``segment`` is the yielding file's ``(st_dev, st_ino)``; ``byte_offset_after``
    is the offset in that file just past the yielded line's newline. Opens the
    segment matching *start*'s ``(dev, ino)`` among ``current`` + ``@*.s``
    archives (or ``current`` at offset 0 when *start* is zero). On EOF it drains
    to the successor: an archive → the next segment at offset 0; the held
    ``current`` whose inode has since changed → the new ``current`` at offset 0
    (held-fd rotation drainage). A *start* segment absent from disk first yields
    a ``(SEGMENT_GAP, 0, b"")`` sentinel, then resumes at ``current`` offset 0.
    """
    start_seg = tuple(start.get("segment", (0, 0)))
    start_off = int(start.get("offset", 0))

    opened = None
    if start_seg == _ZERO_SEG:
        opened = _open_seg(os.path.join(log_dir, "current"), 0)
    else:
        path = _find_segment(log_dir, start_seg)
        if path is None:
            # Retention gap: the turn's start segment rotated out while down.
            yield (SEGMENT_GAP, 0, b"")
            opened = _open_seg(os.path.join(log_dir, "current"), 0)
        else:
            opened = _open_seg(path, start_off)
    if opened is None:
        return

    reader, seg = opened
    try:
        while True:
            line = reader.readline()
            if line.endswith(b"\n"):
                yield (seg, reader.tell(), line)
                continue
            # No complete line: EOF (or a partial tail we do not surface).
            successor = _successor_path(log_dir, seg)
            if successor is None:
                return
            reader.close()
            nxt = _open_seg(successor, 0)
            if nxt is None:
                return
            reader, seg = nxt
    finally:
        try:
            reader.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# The relay.
# ---------------------------------------------------------------------------

SendMessage = Callable[[int, str], Awaitable[int | None]]
EditMessage = Callable[[int, int, str], Awaitable[bool]]
DeleteMessage = Callable[[int, int], Awaitable[bool]]
OnTurnEvent = Callable[[str, dict], Any]
ReplyTexts = Callable[[], Any]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _default_sleep(seconds: float) -> None:  # pragma: no cover - trivial
    import asyncio

    await asyncio.sleep(seconds)


class TopicStreamRelay:
    """Relay one engagement's NDJSON log to its Telegram topic (plain sender).

    Mirrors ``channels.telegram.TopicStreamHandle``'s STRUCTURE (one edited
    message per turn, rolling past ``_MSG_MAX``) but drives injected PLAIN
    senders rather than the rich channel primitive, so it is testable in
    isolation and reusable by the driver (Task 6 wires it in).
    """

    def __init__(
        self,
        *,
        engagement_id: str,
        topic_id: int,
        log_dir: str,
        cursor_path: str,
        send_message: SendMessage,
        edit_message: EditMessage,
        delete_message: DeleteMessage,
        on_turn_event: OnTurnEvent,
        reply_texts: ReplyTexts,
        edit_throttle: float = 1.0,
        sequencer: "OutputSequencer | None" = None,
        _now: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], Awaitable[None]] = _default_sleep,
    ) -> None:
        self.engagement_id = engagement_id
        self.topic_id = topic_id
        self.log_dir = log_dir
        self.cursor_path = cursor_path
        self.send_message = send_message
        self.edit_message = edit_message
        # T3-intended seams: ``delete_message`` (de-dup delete) and
        # ``reply_texts`` (reply-set lookup) are injected but currently UNUSED —
        # §2(d) removed the finalize de-dup delete, and de-dup-before-post moves
        # inside the sequencer when the reply ingress is wired (T3). Retained so
        # the driver wiring is stable across the T2/T3 activation.
        self.delete_message = delete_message
        self.on_turn_event = on_turn_event
        self.reply_texts = reply_texts
        self._edit_throttle = edit_throttle
        self._now = _now
        self._sleep = _sleep
        # v0.79.0 (§2): ALL topic output flows through ONE per-topic sequencer
        # (the single writer that owns the high-water mark, the no-op edit gate
        # and the relay-mediated discrete-posting intent registry). The driver
        # passes the SHARED per-engagement sequencer so discrete ingresses and
        # this relay agree on ordering; an internal one is built when none is
        # injected (isolated relay tests + belt-and-suspenders).
        self.sequencer = sequencer or OutputSequencer(
            engagement_id=engagement_id,
            topic_id=topic_id,
            send_message=send_message,
            edit_message=edit_message,
            _now=_now,
            _sleep=_sleep,
        )

        self.cursor = StreamCursor()
        # Per-turn in-memory state (never persisted — rebuilt on recovery).
        self._turn_text = ""
        self._per_message_text = ""
        self._replay_msg_count = 0
        self._fail_count = 0
        self._dropped = False
        self._drop_warned = False
        self._last_edit_ts = float("-inf")
        # Recovery bookkeeping.
        self._live = True
        self._reconciled = False
        self._passed_cur_seg = False

    # -- persistence helpers ------------------------------------------------

    def _save(self) -> None:
        self.cursor.save(self.cursor_path)

    def _checkpoint(self, seg, off_after: int) -> None:
        """Advance ``current`` past a fully-handled (usually invisible) frame."""
        self.cursor.current = {"segment": list(seg), "offset": off_after}
        self._save()

    def _advance_dropped(self, seg, off_after: int) -> None:
        coord = {"segment": list(seg), "offset": off_after}
        self.cursor.current = coord
        self.cursor.dropped_through = dict(coord)
        self._save()

    def _sync_last_len(self) -> None:
        """Keep ``message_text_lens`` parallel to ``message_ids`` with its last
        entry == the current message's narration length (the additive replay-
        boundary field). Prior entries are frozen at each message's final text
        length, including a SEALED message the live path rolled off of."""
        lens = self.cursor.message_text_lens
        ids = self.cursor.message_ids
        if len(lens) > len(ids):
            del lens[len(ids):]
        while len(lens) < len(ids):
            lens.append(0)
        if ids:
            lens[-1] = len(self._per_message_text)

    # -- coordinate ordering ------------------------------------------------

    def _seg_rank(self, seg: tuple[int, int]) -> int:
        for i, (_p, ident) in enumerate(_ordered_segments(self.log_dir)):
            if ident == seg:
                return i
        return 1 << 30

    def _coord_le(self, seg, off_after: int, target: dict) -> bool:
        tseg = tuple(target.get("segment", (0, 0)))
        toff = int(target.get("offset", 0))
        st = tuple(seg)
        if st == tseg:
            return off_after <= toff
        return self._seg_rank(st) < self._seg_rank(tseg)

    def _update_live(self, seg, off_after: int) -> None:
        """Latch REPLAY→LIVE using monotonic segment order (turn_start<=current)."""
        if self._live:
            return
        cur_seg = tuple(self.cursor.current.get("segment", (0, 0)))
        cur_off = int(self.cursor.current.get("offset", 0))
        st = tuple(seg)
        if st == cur_seg:
            if off_after > cur_off:
                self._live = True
            self._passed_cur_seg = True
        else:
            if self._passed_cur_seg:
                self._live = True

    # -- replay-mode text reconstruction -----------------------------------

    def _replay_text(self, text: str) -> None:
        """Rebuild ``per_message_text`` from a replayed text block — no sends.

        When the checkpoint recorded per-message narration boundaries
        (``message_text_lens``, the additive field), the reconstructed turn text
        is split at THOSE boundaries so a discrete-rollover message keeps only
        its own text (the last message's slice becomes ``per_message_text``).
        Legacy checkpoints (field absent) fall back to _MSG_MAX-only splitting —
        their recovery is already covered by the conservative seal.
        """
        self._turn_text += text
        lens = self.cursor.message_text_lens
        if lens:
            # Boundary-aware: redistribute the whole replayed turn text across
            # the recorded per-message lengths; the trailing slice is the
            # current (last) message's narration.
            full = self._turn_text
            pos = 0
            count = 0
            pmt = ""
            for n in lens:
                if pos >= len(full) and count:
                    break
                pmt = full[pos:pos + n]
                pos += len(pmt)
                count += 1
            if pos < len(full):
                # Residual beyond the recorded boundaries → trailing message
                # (should not occur at the checkpoint; tolerated defensively).
                pmt = pmt + full[pos:]
            self._per_message_text = pmt
            self._replay_msg_count = count
            return
        # Legacy fallback: _MSG_MAX-only reconstruction (unchanged behavior).
        pmt = self._per_message_text
        if self._replay_msg_count == 0:
            head, text = text[:_MSG_MAX], text[_MSG_MAX:]
            pmt = head
            self._replay_msg_count += 1
        else:
            space = _MSG_MAX - len(pmt)
            if space > 0 and text:
                head, text = text[:space], text[space:]
                pmt = pmt + head
        while text:
            piece, text = text[:_MSG_MAX], text[_MSG_MAX:]
            pmt = piece
            self._replay_msg_count += 1
        self._per_message_text = pmt

    async def _reconcile(self) -> None:
        """Recover a lost-before-persist edit for the LAST message (§2:471).

        Only when ``message_ids`` is non-empty (an OPEN turn): a closed-turn
        checkpoint has ``message_ids == []`` so there is nothing to reconcile.
        Routes through ``edit_narration_if_latest`` (§2 sealing across restart,
        option B): on a fresh post-recovery sequencer the checkpoint-named
        message is CONSERVATIVELY SEALED, so the reconciled state posts as a NEW
        closing message rather than editing a message that may have discrete
        posts under it. Any single duplicate is accepted (the documented
        at-least-once risk).
        """
        self._reconciled = True
        if not (self.cursor.message_ids and self._per_message_text):
            return
        try:
            res = await self._apply_seq_edit(
                self.cursor.message_ids[-1], self._per_message_text,
            )
            if res == "sealed":
                applied, mid = await self._apply_op(
                    lambda: self.sequencer.open_narration(self._per_message_text)
                )
                if applied:
                    # Reposted as a single new closing message — collapse the
                    # boundary record to it.
                    self.cursor.message_ids = [mid]
                    self.cursor.message_text_lens = [len(self._per_message_text)]
            else:
                self._sync_last_len()
            self.cursor.last_posted_len = len(self._per_message_text)
            self._save()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "topic stream reconcile edit failed for engagement %s: %s",
                self.engagement_id, exc,
            )

    # -- live posting -------------------------------------------------------

    def _backoff(self) -> float:
        return min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** min(self._fail_count, 5)))

    def _enter_drop(self) -> None:
        if not self._drop_warned:
            self._drop_warned = True
            logger.warning(
                "topic stream for engagement %s: dropping remainder of turn "
                "after %d consecutive Telegram failures",
                self.engagement_id, self._fail_count,
            )
        self._dropped = True

    async def _apply_op(self, factory: Callable[[], Any]) -> tuple[bool, Any]:
        """Run one Telegram op, retrying with bounded backoff until it succeeds
        or the turn crosses ``_DROP_THRESHOLD`` consecutive failures (→ drop).

        Returns ``(applied, result)``; ``applied is False`` means we entered
        drop mode and gave up on this op.
        """
        while True:
            try:
                res = await _maybe_await(factory())
                ok = res is not None and res is not False
            except Exception as exc:  # noqa: BLE001
                ok = False
                res = None
                logger.debug("topic stream op failed (will retry): %s", exc)
            if ok:
                self._fail_count = 0
                return True, res
            self._fail_count += 1
            if self._fail_count >= _DROP_THRESHOLD:
                self._enter_drop()
                return False, None
            await self._sleep(self._backoff())

    async def _apply_seq_edit(self, msg_id: int, value: str) -> str:
        """Route a narration edit through the sequencer's
        ``edit_narration_if_latest`` while honoring the at-least-once
        retry/drop contract. Returns ``"applied"`` | ``"sealed"`` |
        ``"dropped"``.

        ``SEALED`` (something posted below this message — §2 rollover) is NOT a
        failure: the caller opens a fresh narration message for the pending
        text. Only a ``FAILED`` wire edit counts toward the drop threshold.
        """
        while True:
            try:
                res = await _maybe_await(
                    self.sequencer.edit_narration_if_latest(msg_id, value)
                )
            except Exception as exc:  # noqa: BLE001
                res = FAILED
                logger.debug("topic stream edit op failed (will retry): %s", exc)
            if res == APPLIED:
                self._fail_count = 0
                return "applied"
            if res == SEALED:
                self._fail_count = 0
                return "sealed"
            self._fail_count += 1
            if self._fail_count >= _DROP_THRESHOLD:
                self._enter_drop()
                return "dropped"
            await self._sleep(self._backoff())

    def _plan_ops(self, text: str) -> list[tuple[str, str]]:
        """Ops to render *text* appended to the current message with rollover.

        First text of a turn → one ``send``; otherwise fill the current message
        via one ``edit``; any surplus past ``_MSG_MAX`` splits into ``send``s.
        """
        ops: list[tuple[str, str]] = []
        pmt = self._per_message_text
        if not self.cursor.message_ids:
            head, text = text[:_MSG_MAX], text[_MSG_MAX:]
            ops.append(("send", head))
            pmt = head
        else:
            space = _MSG_MAX - len(pmt)
            if space > 0 and text:
                head, text = text[:space], text[space:]
                pmt = pmt + head
                ops.append(("edit", pmt))
        while text:
            piece, text = text[:_MSG_MAX], text[_MSG_MAX:]
            ops.append(("send", piece))
            pmt = piece
        return ops

    async def _execute_ops(self, ops: list[tuple[str, str]]) -> None:
        """Render *ops* (send/edit) through the sequencer. Sets ``self._dropped``
        on a drop; NEVER checkpoints (the caller owns cursor advancement).

        An ``edit`` that returns SEALED opens a NEW narration message for the
        pending increment — §2 rollover-on-interleave / conservative recovery
        seal."""
        for kind, value in ops:
            if self._dropped:
                return
            if kind == "send":
                applied, mid = await self._apply_op(
                    lambda v=value: self.sequencer.open_narration(v)
                )
                if not applied:
                    return
                self.cursor.message_ids.append(mid)
                self._per_message_text = value
            else:  # edit
                prior = self._per_message_text
                res = await self._apply_seq_edit(
                    self.cursor.message_ids[-1], value
                )
                if res == "dropped":
                    return
                if res == "sealed":
                    increment = (
                        value[len(prior):] if value.startswith(prior) else value
                    )
                    applied, mid = await self._apply_op(
                        lambda v=increment: self.sequencer.open_narration(v)
                    )
                    if not applied:
                        return
                    self.cursor.message_ids.append(mid)
                    self._per_message_text = increment
                else:  # applied
                    self._per_message_text = value
                self._last_edit_ts = self._now()
            # Record this op's per-message narration boundary (send appended a
            # new message; edit grew the current or rolled to a fresh one).
            self._sync_last_len()

    async def _post_text(self, text: str, seg, off_after: int) -> None:
        """Text-only streaming path: throttle + single frame-end checkpoint."""
        self._turn_text += text
        if self._dropped:
            self._advance_dropped(seg, off_after)
            return

        ops = self._plan_ops(text)
        if not ops:
            self._checkpoint(seg, off_after)
            return

        # Throttle only the rapid single-edit streaming case: skip the live
        # edit within the window (text is retained in memory and flushed by the
        # next edit or at finalize), and do NOT advance the cursor.
        if len(ops) == 1 and ops[0][0] == "edit":
            if self._now() - self._last_edit_ts < self._edit_throttle:
                self._per_message_text = ops[0][1]
                return

        await self._execute_ops(ops)
        if self._dropped:
            self._advance_dropped(seg, off_after)
            return

        self.cursor.current = {"segment": list(seg), "offset": off_after}
        self.cursor.last_posted_len = len(self._per_message_text)
        self._save()

    async def _append_narration(self, text: str) -> None:
        """Append narration text WITHOUT throttle or checkpoint (used by the
        block-ordered mixed-frame path, which checkpoints once at frame end)."""
        self._turn_text += text
        if self._dropped:
            return
        ops = self._plan_ops(text)
        await self._execute_ops(ops)

    async def _match_discrete_block(self, name: str, tool_input: dict) -> None:
        """Drive relay-mediated discrete matching for ONE tool_use block (§2(3)).

        Computes the block's ``(tool, projection_hash)`` under the pinned
        projection and asks the sequencer to resolve it at this position. An
        armed intent posts here (sealing narration); a tombstone / debt consumes
        the block silently; a pending/absent hold-eligible intent may hold the
        slot. The T1-stubbed state (no intents registered) resolves to
        ``no_match`` instantly, so this is inert until T2/T3 wire the
        ingresses."""
        try:
            block_hash = projection_hash(name, tool_input)
        except ValueError:
            # Non-serializable tool_input can never match a hashed intent.
            return
        try:
            await self.sequencer.post_for_block(name, block_hash)
        except Exception as exc:  # noqa: BLE001 — discrete posting is best-effort
            logger.warning(
                "topic stream discrete-post match failed for engagement %s "
                "(tool=%s): %s", self.engagement_id, name, exc,
            )

    async def _handle_assistant_blocks(
        self, blocks: list[tuple], seg, off_after: int,
    ) -> None:
        """Process a tool-bearing assistant frame block-by-block (§2(3)).

        Text blocks append to narration; each tool_use block fires the
        mutating-tool event (once, for the first mutating tool) and drives
        relay-mediated discrete matching AT ITS POSITION. Checkpoints ONCE at
        frame end."""
        if self._dropped:
            self._advance_dropped(seg, off_after)
            return
        fired_mutating = False
        for block in blocks:
            if block[0] == "text":
                await self._append_narration(block[1])
                if self._dropped:
                    self._advance_dropped(seg, off_after)
                    return
            else:  # tool_use
                _kind, name, tool_input = block
                if not fired_mutating and name not in _NON_MUTATING_TOOLS:
                    fired_mutating = True
                    await _maybe_await(
                        self.on_turn_event("mutating_tool", {"tool": name})
                    )
                await self._match_discrete_block(name, tool_input)

        self.cursor.current = {"segment": list(seg), "offset": off_after}
        self.cursor.last_posted_len = len(self._per_message_text)
        self._save()

    def _reset_turn_state(self) -> None:
        self._turn_text = ""
        self._per_message_text = ""
        self._replay_msg_count = 0
        self._fail_count = 0
        self._dropped = False
        self._drop_warned = False
        self._last_edit_ts = float("-inf")

    async def _finalize(self, seg, off_after: int) -> None:
        """Route the closing edit through ``edit_narration_if_latest`` (§2:612),
        then persist a CLOSED-TURN checkpoint (``message_ids=[]``,
        ``turn_start == current`` past ``result``).

        §2(d): the reply de-dup DELETE (formerly here at :633) is REMOVED — no
        message is ever deleted; a duplicate is preferred over erasing history.
        De-dup, when wired (T3), happens BEFORE posting inside the sequencer.
        §2(c): if the narration message was SEALED (a discrete post below it, or
        a conservative seal after process recovery), the closing state posts as
        a NEW message instead of editing one with content under it.
        """
        coord = {"segment": list(seg), "offset": off_after}
        if not self._dropped and self.cursor.message_ids and self._per_message_text:
            # B2 (Sol r1): the closing edit carries this turn's FINAL fragment,
            # so it honors the at-least-once retry/drop contract via
            # ``_apply_seq_edit``. Only after it lands (APPLIED/SEALED-then-new),
            # or the turn crosses the drop threshold, may the closed-turn
            # checkpoint below advance past ``result``.
            res = await self._apply_seq_edit(
                self.cursor.message_ids[-1], self._per_message_text,
            )
            if res == "sealed":
                applied, mid = await self._apply_op(
                    lambda: self.sequencer.open_narration(self._per_message_text)
                )
                if applied:
                    self.cursor.message_ids = [mid]

        if self._dropped:
            # The turn ended in drop mode: ``dropped_through`` reaches the
            # turn's TERMINAL coordinate so a restart never replays the tail.
            self.cursor.dropped_through = dict(coord)
        self.cursor.current = coord
        self.cursor.turn_start = dict(coord)
        self.cursor.message_ids = []
        self.cursor.message_text_lens = []
        self.cursor.last_posted_len = 0
        self._reset_turn_state()
        # §2(6): prune intents/tombstones/id→outcome + seal narration at turn end.
        self.sequencer.prune_turn()
        await self.sequencer.seal_narration()
        self._save()

    # -- main loop ----------------------------------------------------------

    async def run(self) -> None:
        self.cursor = StreamCursor.load(self.cursor_path)
        cur_seg = tuple(self.cursor.current.get("segment", (0, 0)))
        recovering = (
            cur_seg != _ZERO_SEG
            or bool(self.cursor.message_ids)
            or self.cursor.dropped_through is not None
        )
        self._live = not recovering
        self._reconciled = False
        self._passed_cur_seg = False
        self._reset_turn_state()

        async for seg, off_after, raw in iter_log_segments(
            self.log_dir, self.cursor.turn_start
        ):
            if seg == SEGMENT_GAP:
                logger.warning(
                    "topic stream retention gap for engagement %s: turn_start "
                    "segment %s absent on disk; resuming at current offset 0",
                    self.engagement_id, self.cursor.turn_start.get("segment"),
                )
                # The turn's history is unrecoverable — resume fresh and live.
                self._live = True
                self._reconciled = True
                self.cursor.message_ids = []
                self.cursor.message_text_lens = []
                self._reset_turn_state()
                continue

            # Drop-mode tail from a prior run: skip entirely (no side effects).
            if self.cursor.dropped_through is not None and self._coord_le(
                seg, off_after, self.cursor.dropped_through
            ):
                continue

            was_live = self._live
            self._update_live(seg, off_after)
            if self._live and not was_live and not self._reconciled:
                await self._reconcile()

            await self._handle_frame(seg, off_after, raw)

        # Stream ended while still replaying an open turn — reconcile now.
        if recovering and not self._reconciled:
            await self._reconcile()

    async def _handle_frame(self, seg, off_after: int, raw: bytes) -> None:
        frame = parse_frame(raw)

        if not self._live:
            # REPLAY: rebuild visible-text state ONLY; suppress all side effects.
            if frame is not None and frame.get("type") == "assistant":
                texts = extract_text_blocks(frame)
                if texts:
                    self._replay_text("".join(texts))
            return

        if frame is None:
            logger.debug(
                "topic stream: skipping non-JSON line for engagement %s",
                self.engagement_id,
            )
            self._checkpoint(seg, off_after)
            return

        if frame.get("casa_control") == "spawn":
            await _maybe_await(
                self.on_turn_event("spawn", {"epoch": frame.get("epoch")})
            )
            self._checkpoint(seg, off_after)
            return

        ftype = frame.get("type")

        if ftype == "system" and frame.get("subtype") == "init":
            off_before = off_after - len(raw)
            self.cursor.turn_start = {"segment": list(seg), "offset": off_before}
            self.cursor.message_ids = []
            self.cursor.message_text_lens = []
            self.cursor.last_posted_len = 0
            self._reset_turn_state()
            await _maybe_await(
                self.on_turn_event(
                    "turn_start", {"session_id": frame.get("session_id")}
                )
            )
            self._checkpoint(seg, off_after)
            return

        if ftype == "assistant":
            blocks = iter_content_blocks(frame)
            if any(b[0] == "tool_use" for b in blocks):
                # Mixed / tool-bearing frame: process blocks IN ORDER so a
                # relay-mediated discrete post lands at exactly its content-
                # block position (§2(3)), interleaved with narration text.
                await self._handle_assistant_blocks(blocks, seg, off_after)
                return
            # Text-only frame (the common streaming case): unchanged fast path —
            # join, throttle, single checkpoint.
            texts = [b[1] for b in blocks if b[0] == "text"]
            if texts:
                await self._post_text("".join(texts), seg, off_after)
                return
            # No visible text (e.g. thinking-only) → invisible checkpoint.
            self._checkpoint(seg, off_after)
            return

        if ftype == "result":
            await self._finalize(seg, off_after)
            await _maybe_await(
                self.on_turn_event("result", {"subtype": frame.get("subtype")})
            )
            return

        # rate_limit_event / unknown type → invisible checkpoint.
        self._checkpoint(seg, off_after)
