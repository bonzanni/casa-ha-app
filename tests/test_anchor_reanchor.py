"""Task 9 / v0.83.0 §A3(b) — anchor re-anchor: staged flow, boundary-complete
latch, retry owner, terminal-finalize settle, boot reconcile owner (F-ORDER).

Enforces the operator invariant *"once asked, the free-text question stays the
LAST item until answered"*. A doctrine-defying agent keeps narrating below its
own open anchor; at every turn boundary the driver re-anchors the oldest
unanswered anchor via the 4-step staged flow (stage → post_discrete → persist
mid → settle old copy). A per-engagement latch carries the obligation across
abnormal / idle boundaries; a bounded-backoff retry owner completes it when a
boundary is the last event; terminal finalize SETTLES instead of re-anchoring;
and a boot reconciliation owner settles refused/terminal records after Telegram
readiness.

REAL registry over a tmp tombstone + REAL OutputSequencer (fake wire fns) +
REAL driver methods. Injected clocks; NEVER patches ``<module>.asyncio.sleep``
(the shared attribute — the memory-cage rule)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ---------------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------------


class _Wire:
    """One ordered fake wire backing BOTH the text send (``send_to_topic`` /
    platform notices) and the markup send (``post_discrete``), so message
    ordering is a single monotonic mid sequence. ``markup_ok=False`` makes
    ``post_discrete`` fail (wire down); ``edit_ok=False`` makes every settle
    edit transiently fail (unconfirmed)."""

    def __init__(self, *, markup_ok: bool = True, edit_ok: bool = True,
                 start: int = 1000):
        self._n = start
        self.posts: list[tuple[str, int, str]] = []   # (kind, mid, text)
        self.edits: list[tuple[str, int, str]] = []    # (kind, mid, text)
        self.markup_ok = markup_ok
        self.edit_ok = edit_ok

    def _mid(self) -> int:
        self._n += 1
        return self._n

    async def send_text(self, topic, text, **kw) -> int:
        mid = self._mid()
        self.posts.append(("text", mid, text))
        return mid

    async def send_markup(self, topic, text, markup, reply_to=None):
        if not self.markup_ok:
            return None
        mid = self._mid()
        self.posts.append(("markup", mid, text))
        return mid

    async def edit_text(self, topic, mid, text, clear_keyboard=False) -> bool:
        self.edits.append(("text", mid, text))
        return self.edit_ok

    async def edit_markup(self, topic, mid, text, markup) -> bool:
        self.edits.append(("markup", mid, text))
        return self.edit_ok

    def post_mids(self) -> list[int]:
        return [mid for _, mid, _ in self.posts]

    def edit_mids(self) -> list[int]:
        return [mid for _, mid, _ in self.edits]


async def _noop_sleep(_delay: float) -> None:
    return None


async def _make_registry(tmp_path: Path, *, status: str = "active"):
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 77}, topic_id=555)
    if status != "active":
        # Drive a terminal/idle status directly for the boot-owner tests.
        rec.status = status
    return reg, rec


async def _add_anchor(reg, rec, *, mid, text="Q1: DB name?"):
    n = await reg.allocate_question_number(rec.id)
    await reg.add_open_question(rec.id, n, mid, text=text, kind="anchor")
    return n


def _make_driver(tmp_path: Path, reg, wire, *, retry_sleep=None):
    from drivers.claude_code_driver import ClaudeCodeDriver

    return ClaudeCodeDriver(
        engagements_root=str(tmp_path / "eng"),
        send_to_topic=wire.send_text,
        casa_framework_mcp_url="http://x",
        edit_topic_message=wire.edit_text,
        send_topic_message_markup=wire.send_markup,
        edit_topic_message_markup=wire.edit_markup,
        registry=reg,
        sleep=_noop_sleep,
        reanchor_retry_sleep=retry_sleep,
    )


def _entry(reg, rec, n):
    for q in reg.open_question_entries(rec.id):
        if q.get("n") == n:
            return q
    return None


# ===========================================================================
# 1. the staged 4-step re-anchor happy path
# ===========================================================================


class TestStagedFlowHappyPath:
    async def test_reanchor_reposts_below_and_settles_old(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600  # narration posted below the anchor this turn

        ok = await drv._reanchor_pass(rec)

        assert ok is True
        # New copy posted through the sequencer (markup wire), high-water advanced.
        assert len(wire.posts) == 1 and wire.posts[0][0] == "markup"
        new_mid = wire.posts[0][1]
        assert seq.high_water == new_mid
        # Ledger now tracks the new copy; stale_mids emptied after the confirmed
        # old-copy settle.
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == new_mid
        assert e["stale_mids"] == []
        # Old copy settled to the re-posted-below marker.
        assert any(mid == 500 and "re-posted below" in text
                   for _, mid, text in wire.edits)

    async def test_already_last_is_noop(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=700)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 700  # anchor IS the last item

        ok = await drv._reanchor_pass(rec)

        assert ok is True
        assert wire.posts == [] and wire.edits == []

    async def test_no_open_anchor_is_noop(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 900

        assert await drv._reanchor_pass(rec) is True
        assert wire.posts == []


# ===========================================================================
# 2. per-step persist-failure injection (spec §A3(b) steps 1-4)
# ===========================================================================


class TestStagedFlowPerStepFailures:
    async def _setup(self, tmp_path, **wire_kw):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire(**wire_kw)
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        return reg, rec, n, wire, drv, seq

    async def test_step1_stage_failure_nothing_on_wire(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        reg.stage_stale_mid = AsyncMock(side_effect=RuntimeError("disk"))

        ok = await drv._reanchor_pass(rec)

        assert ok is False               # retry owed
        assert wire.posts == []          # nothing on the wire
        assert _entry(reg, rec, n)["tg_message_id"] == 500  # original intact

    async def test_step2_wire_failure_unstages_and_owes_retry(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path, markup_ok=False)

        ok = await drv._reanchor_pass(rec)

        assert ok is False               # wire down → retry owed
        assert wire.posts == []          # post_discrete returned None
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == 500
        assert e["stale_mids"] == []     # step-1 stage rolled back (un-staged)

    async def test_step3_persist_failure_settles_new_copy_see_above(self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path)
        reg.update_question_mid = AsyncMock(side_effect=RuntimeError("disk"))

        ok = await drv._reanchor_pass(rec)

        assert ok is False
        new_mid = wire.posts[0][1]
        # New copy was posted then settled to '↪ see the question above'.
        assert any(mid == new_mid and "see the question above" in text
                   for _, mid, text in wire.edits)
        # Original stays live + tracked; stale_mids keeps old_mid (overlap-tolerant).
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == 500
        assert 500 in e["stale_mids"]

    async def test_step4_unconfirmed_retains_stale_mid_but_obligation_met(
            self, tmp_path):
        reg, rec, n, wire, drv, seq = await self._setup(tmp_path, edit_ok=False)

        ok = await drv._reanchor_pass(rec)

        assert ok is True                # obligation met (question now LAST)
        new_mid = wire.posts[0][1]
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == new_mid      # step-3 persisted
        assert e["stale_mids"] == [500]           # step-4 unconfirmed → retained


# ===========================================================================
# 3. unconfirmed step-4 → boot reconcile settles BOTH mids
# ===========================================================================


class TestUnconfirmedStep4Reconcile:
    async def test_reconcile_settles_current_and_stale(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire(edit_ok=False)
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        await drv._reanchor_pass(rec)             # step-4 unconfirmed
        new_mid = wire.posts[0][1]
        assert _entry(reg, rec, n)["stale_mids"] == [500]

        # "Boot": the transport recovers, reconcile settles every tracked copy.
        wire.edit_ok = True
        snapshot = reg.open_question_entries(rec.id)
        await drv.reconcile_open_questions(rec, snapshot)

        settled = set(wire.edit_mids())
        assert 500 in settled and new_mid in settled   # both mids settled
        assert reg.open_question_entries(rec.id) == []  # entry removed


# ===========================================================================
# 4. mid-re-anchor answer → revalidate declines → un-stage, no post
# ===========================================================================


class TestMidReanchorAnswer:
    async def test_answer_during_stage_declines_send(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600

        # The operator answer lands DURING the awaited step-1 stage: reserve the
        # anchor right after the stale-mid is staged.
        orig_stage = reg.stage_stale_mid

        async def _staging(*a, **k):
            r = await orig_stage(*a, **k)
            drv.reserve_answer(rec.id)   # answer arrives mid-stage
            return r

        reg.stage_stale_mid = _staging

        ok = await drv._reanchor_pass(rec)

        assert ok is True                # the answer won — nothing owed
        assert wire.posts == []          # revalidation declined the send
        e = _entry(reg, rec, n)
        assert e["tg_message_id"] == 500          # original copy intact
        assert e["stale_mids"] == []              # staged mid un-staged
        # The question stays reserved/answered (invisible to gates/re-anchor).
        assert drv._effective_open_question_numbers(rec.id) == []


# ===========================================================================
# 4b. B2 — the Sol interleaving: an answer settlement racing a re-anchor caught
#     between step-2 (new copy posted) and step-3 (mid persisted) must serialize
#     behind the maintenance lock and settle the NEW current copy; the entry
#     never closes with a live untracked copy.
# ===========================================================================


class TestB2SettleInterleaving:
    async def test_answer_settle_serializes_after_reanchor_step3(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600   # content posted below the anchor this turn

        # Event-gate step 3 (update_question_mid): re-anchor gets caught between
        # POSTING the new copy (step 2 accepted) and PERSISTING its mid (step 3).
        gate = asyncio.Event()
        orig_update = reg.update_question_mid

        async def _gated_update(eid, num, new_mid):
            await gate.wait()
            return await orig_update(eid, num, new_mid)

        reg.update_question_mid = _gated_update

        ra = asyncio.ensure_future(drv._reanchor_pass(rec))
        await asyncio.sleep(0.02)
        # Step 2 accepted: the NEW copy is on the wire (markup), high-water moved.
        assert len(wire.posts) == 1 and wire.posts[0][0] == "markup"
        new_mid = wire.posts[0][1]
        assert seq.high_water == new_mid

        # The operator answer arrives + settles — must BLOCK on the maintenance
        # lock re-anchor still holds (it is mid-step-3), not race an old snapshot.
        settle = asyncio.ensure_future(drv._promote_answer_on_enqueue(rec))
        await asyncio.sleep(0.02)
        assert not settle.done()          # serialized BEHIND the re-anchor

        gate.set()
        assert await ra is True
        await settle

        # The settle edited the NEW current copy (post step-3) with ✅ answered,
        # and the entry closed with NO live untracked copy left behind.
        assert _entry(reg, rec, n) is None
        assert any(mid == new_mid and "answered below" in text
                   for _, mid, text in wire.edits)
        # The OLD copy was re-anchor-settled (re-posted-below), never left live.
        assert any(mid == 500 for _, mid, _ in wire.edits)


# ===========================================================================
# 4c. M4 — entry-removal invariant: a failed unstage / failed close RETAINS the
#     entry (never closes with a durable stale_mid; strict close rolls back).
# ===========================================================================


class TestM4EntryRemovalInvariant:
    async def test_failed_unstage_retains_entry_with_mid_staged(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        await reg.stage_stale_mid(rec.id, n, 400)   # a staged OLD copy
        wire = _Wire()                              # edit_ok=True → both confirm
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)

        async def _boom(*a, **k):
            raise RuntimeError("unstage persist down")

        reg.unstage_stale_mid = _boom

        snapshot = reg.open_question_entries(rec.id)
        await drv.reconcile_open_questions(rec, snapshot)

        # M4: the confirmed stale copy's strict un-stage RAISED, so the entry must
        # NOT close — it is retained with the stale mid still staged for retry.
        e = _entry(reg, rec, n)
        assert e is not None
        assert 400 in (e.get("stale_mids") or [])

    async def test_failed_close_retains_entry(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)

        # The STRICT close's tombstone write fails → rollback + raise; the driver
        # treats the raise as RETAINED.
        orig = reg._write_tombstone_locked

        async def _fail_strict(*, strict=False):
            if strict:
                raise RuntimeError("disk full")
            return await orig(strict=strict)

        reg._write_tombstone_locked = _fail_strict

        snapshot = reg.open_question_entries(rec.id)
        await drv.reconcile_open_questions(rec, snapshot)

        assert _entry(reg, rec, n) is not None      # retained, not dropped

    async def test_close_open_question_strict_rolls_back_and_raises(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        orig = reg._write_tombstone_locked

        async def _fail_strict(*, strict=False):
            if strict:
                raise RuntimeError("disk full")
            return await orig(strict=strict)

        reg._write_tombstone_locked = _fail_strict

        with pytest.raises(RuntimeError):
            await reg.close_open_question(rec.id, n)
        # Rolled back full-tuple: the entry survives in memory (never split).
        assert _entry(reg, rec, n) is not None


# ===========================================================================
# 5. step-2→3 crash residual: tracked copies settled, orphan documented
# ===========================================================================


class TestCrashResidual:
    async def test_crash_between_post_and_persist_leaves_one_orphan(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)

        # Simulate the exact step-1 + step-2 partial: stale staged, new copy on
        # the wire, then a crash BEFORE update_question_mid persists.
        await reg.stage_stale_mid(rec.id, n, 500)
        orphan_mid = await seq.post_discrete("Q1: DB name?")
        assert orphan_mid is not None
        # (update_question_mid never runs — crash)

        # Boot reconciliation settles the TRACKED copies (current 500 + stale 500);
        # the orphan (new copy) is never edited.
        snapshot = reg.open_question_entries(rec.id)
        await drv.reconcile_open_questions(rec, snapshot)

        assert 500 in wire.edit_mids()
        assert orphan_mid not in wire.edit_mids()   # orphan left as one stale line
        assert reg.open_question_entries(rec.id) == []


# ===========================================================================
# 6. boundary consumers
# ===========================================================================


class _FakeSpool:
    """Minimal spool stub: ``on_turn_end`` flushes a pending platform notice
    through the sequencer (advancing high-water) — used to pin the result-consumer
    ordering (notice BEFORE re-anchor)."""

    def __init__(self, seq, *, notice: str | None = None):
        self.seq = seq
        self.notice = notice
        self.notice_mid = None

    async def on_turn_end(self):
        if self.notice is not None:
            self.notice_mid = await self.seq.post_platform_notice(self.notice)

    async def on_spawn(self):
        return None

    async def on_turn_start(self):
        return None


class TestBoundaryConsumers:
    async def test_result_runs_reanchor_after_on_turn_end(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        drv._inbound[rec.id] = _FakeSpool(seq, notice="pending receipt")

        await drv._on_stream_event(rec, "result", {})

        # The flushed notice (text) posted FIRST, then the re-anchor copy (markup)
        # — the re-anchored question ends up LAST.
        assert [k for k, _, _ in wire.posts] == ["text", "markup"]
        notice_mid, reanchor_mid = wire.post_mids()
        assert notice_mid < reanchor_mid
        assert seq.high_water == reanchor_mid
        assert _entry(reg, rec, n)["tg_message_id"] == reanchor_mid

    async def test_spawn_without_result_consumes(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        # A prior epoch is pending a result → the next spawn is an abnormal
        # (spawn-without-result) boundary.
        drv._epoch_pending[rec.id] = 1

        await drv._on_stream_event(rec, "spawn", {"epoch": 2})

        assert len(wire.posts) == 1 and wire.posts[0][0] == "markup"
        assert _entry(reg, rec, n)["tg_message_id"] == wire.posts[0][1]

    async def test_spawn_with_no_prior_epoch_does_not_reanchor(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        drv._epoch_pending[rec.id] = None   # no prior epoch → normal first spawn

        await drv._on_stream_event(rec, "spawn", {"epoch": 1})

        assert wire.posts == []             # no abnormal boundary → no re-anchor

    async def test_rollback_completion_consumer_reanchors_idle_engagement(
            self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        seq = drv._ensure_sequencer(rec)
        # An idle engagement: the operator's /silent posted a command notice
        # (below the anchor), then its reservation rolls back — no future spawn.
        token = drv.reserve_answer(rec.id)
        await seq.post_platform_notice("Observer quieted.")   # command notice
        rolled = await drv.rollback_answer_reservation(rec.id, token)

        assert rolled is True
        # The rollback consumer (d) ran the pass → the anchor is LAST again.
        assert any(k == "markup" for k, _, _ in wire.posts)
        reanchor_mid = [mid for k, mid, _ in wire.posts if k == "markup"][-1]
        assert seq.high_water == reanchor_mid
        assert _entry(reg, rec, n)["tg_message_id"] == reanchor_mid


# ===========================================================================
# 7. terminal finalize SETTLES anchors (never re-anchors)
# ===========================================================================


class TestTerminalFinalizeSettle:
    async def test_settle_all_open_questions_cancelled(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)

        await drv.settle_all_open_questions(rec, "cancelled")

        # The live anchor is SETTLED (closed copy) — never re-anchored (no post).
        assert wire.posts == []
        assert any(mid == 500 and "engagement ended" in text
                   for _, mid, text in wire.edits)
        assert reg.open_question_entries(rec.id) == []

    async def test_answered_entry_gets_check_copy(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        await reg.mark_question_answered(rec.id, n)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)

        await drv.settle_all_open_questions(rec, "completed")

        assert any(mid == 500 and "answered below" in text
                   for _, mid, text in wire.edits)
        assert reg.open_question_entries(rec.id) == []

    async def test_terminal_settle_clears_latch_and_cancels_retry(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)
        # Arm the latch + a retry owner as if a prior pass had failed.
        drv.set_reanchor_due(rec.id)
        drv._arm_reanchor_retry(rec)
        assert rec.id in drv._reanchor_retry_tasks

        await drv.settle_all_open_questions(rec, "cancelled")

        assert rec.id not in drv._reanchor_due
        assert rec.id not in drv._reanchor_retry_tasks


# ===========================================================================
# 8. retry owner (Sol §6n note 1)
# ===========================================================================


class TestRetryOwner:
    async def test_first_pass_fails_then_retry_succeeds(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=500)
        delays: list[float] = []

        async def _rec_sleep(d):
            delays.append(d)

        wire = _Wire(markup_ok=False)   # wire down → first pass fails
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_rec_sleep)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600

        # A boundary consumer runs the (failing) pass → latch set + retry armed.
        await drv._consume_reanchor(rec)
        assert rec.id in drv._reanchor_due
        task = drv._reanchor_retry_tasks.get(rec.id)
        assert task is not None

        # Transport recovers; drive the retry loop to completion.
        wire.markup_ok = True
        await task

        assert delays == [5.0]                       # one backoff step
        assert rec.id not in drv._reanchor_due       # latch cleared on success
        assert rec.id not in drv._reanchor_retry_tasks  # completed → no entry
        assert _entry(reg, rec, n)["tg_message_id"] == wire.posts[0][1]

    async def test_repeated_failures_walk_backoff_to_cap(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)
        delays: list[float] = []

        async def _cap_sleep(d):
            delays.append(d)
            if len(delays) >= 4:
                raise asyncio.CancelledError   # simulate teardown at the cap

        wire = _Wire(markup_ok=False)   # stays down → every pass fails
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_cap_sleep)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600

        await drv._consume_reanchor(rec)
        task = drv._reanchor_retry_tasks.get(rec.id)
        with pytest.raises(asyncio.CancelledError):
            await task

        # 5 → 30 → 300 → 300 (capped, repeated), then the CancelledError.
        assert delays == [5.0, 30.0, 300.0, 300.0]
        assert rec.id in drv._reanchor_due   # never cleared (never succeeded)

    async def test_double_arm_is_noop(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)

        gate = asyncio.Event()

        async def _blocking_sleep(_d):
            await gate.wait()

        wire = _Wire(markup_ok=False)
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_blocking_sleep)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        drv.set_reanchor_due(rec.id)

        drv._arm_reanchor_retry(rec)
        first = drv._reanchor_retry_tasks.get(rec.id)
        drv._arm_reanchor_retry(rec)                 # double-arm
        second = drv._reanchor_retry_tasks.get(rec.id)
        assert first is second                       # one task per engagement

        gate.set()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

    async def test_teardown_cancels_retry_no_reschedule(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)

        gate = asyncio.Event()
        after_cancel: list[float] = []

        async def _blocking_sleep(_d):
            await gate.wait()
            after_cancel.append(_d)   # must never run again after cancel

        wire = _Wire(markup_ok=False)
        drv = _make_driver(tmp_path, reg, wire, retry_sleep=_blocking_sleep)
        seq = drv._ensure_sequencer(rec)
        seq._high_water = 600
        drv.set_reanchor_due(rec.id)
        drv._arm_reanchor_retry(rec)
        task = drv._reanchor_retry_tasks.get(rec.id)

        await drv.cancel(rec)   # teardown cancels the retry owner

        assert rec.id not in drv._reanchor_retry_tasks
        with pytest.raises(asyncio.CancelledError):
            await task
        assert after_cancel == []


# ===========================================================================
# 9. boot reconciliation owner (readiness barrier + claimed-set)
# ===========================================================================


class TestBootReconcileOwner:
    async def test_reconcile_waits_for_readiness(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)
        ready = asyncio.Event()
        snapshot = reg.open_question_entries(rec.id)

        task = drv.schedule_boot_reconcile(rec, snapshot, ready)
        # Give the task a chance to run — it MUST block on readiness.
        await asyncio.sleep(0)
        assert wire.edits == []                 # nothing settled before readiness

        ready.set()
        await task
        assert 500 in wire.edit_mids()          # settled only after readiness
        assert reg.open_question_entries(rec.id) == []

    async def test_claimed_set_prevents_double_reconcile(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)
        ready = asyncio.Event()
        ready.set()
        claimed: set[str] = set()
        snapshot = reg.open_question_entries(rec.id)

        t1 = drv.schedule_boot_reconcile(rec, snapshot, ready, claimed=claimed)
        t2 = drv.schedule_boot_reconcile(rec, snapshot, ready, claimed=claimed)
        assert t1 is not None and t2 is None    # second claim refused
        await t1

    async def test_empty_snapshot_is_noop(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        assert drv.schedule_boot_reconcile(rec, [], asyncio.Event()) is None

    async def test_delayed_readiness_settles_on_first_boot(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=500)
        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(rec)
        ready = asyncio.Event()
        snapshot = reg.open_question_entries(rec.id)
        task = drv.schedule_boot_reconcile(rec, snapshot, ready)

        # Readiness lands late (transport recovered after several rebuild retries).
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert wire.edits == []
        ready.set()
        await task
        assert 500 in wire.edit_mids()          # settles on THIS boot, not "never"


# ===========================================================================
# 10. boot replay owner integration (refused / terminal records + snapshot)
# ===========================================================================


class TestBootReplayOwner:
    async def test_terminal_and_fresh_ask_and_no_double(self, tmp_path, monkeypatch):
        import casa_core

        # Two records: a TERMINAL record with a stranded question, and an
        # active/idle undergoing record whose FRESH same-process ask must NOT be
        # captured + expired by the pre-service snapshot.
        from engagement_registry import EngagementRegistry
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        term = await reg.create("executor", "configurator", "claude_code", "t",
                                {"user_id": 1}, topic_id=111)
        term.status = "cancelled"
        await reg.add_open_question(term.id, 1, 900, text="Q1: old?",
                                    kind="anchor")

        wire = _Wire()
        drv = _make_driver(tmp_path, reg, wire)
        drv._ensure_sequencer(term)

        ready = asyncio.Event()

        # No undergoing records → the s6 fast-path return fires; the terminal
        # owner MUST still be scheduled (pre-lock). Stub s6 heal surface.
        scheduled: list[str] = []
        orig = drv.schedule_boot_reconcile

        def _spy(rec, snap, tr, *, claimed=None):
            scheduled.append(rec.id)
            return orig(rec, snap, tr, claimed=claimed)

        drv.schedule_boot_reconcile = _spy

        await casa_core.replay_undergoing_engagements(
            registry=reg, driver=drv, executor_registry=None,
            engagements_root=str(tmp_path / "eng"), telegram_ready=ready)

        # Terminal record with a stranded question was claimed for reconcile,
        # gated on readiness (nothing settled yet).
        assert term.id in scheduled
        assert wire.edits == []
        ready.set()
        # Drain the scheduled reconcile task.
        for t in list(drv._boot_reconcile_tasks):
            await t
        assert 900 in wire.edit_mids()
        assert reg.open_question_entries(term.id) == []

    async def test_active_fresh_ask_between_snapshot_and_attach_not_expired(
        self, tmp_path, monkeypatch,
    ):
        # B3: an ACTIVE (undergoing) record with NO open questions at the
        # PRE-SERVICE snapshot. The resumed CLI registers a fresh ask BETWEEN the
        # snapshot and attach (``start_service`` here) — casa_core must pass the
        # per-record snapshot as ``[]`` (NOT missing/None), so ``_spawn_background_
        # tasks`` reconciles NOTHING and the fresh ask is never expired.
        import casa_core
        from drivers import s6_rc
        from engagement_registry import EngagementRegistry
        from unittest.mock import AsyncMock, MagicMock

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))
        # Skip the heal machinery: pretend the service pair is complete + current.
        monkeypatch.setattr(s6_rc, "service_pair_complete", lambda **kw: True)
        monkeypatch.setattr(s6_rc, "run_script_is_stale", lambda **kw: False)
        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", AsyncMock())

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create("executor", "configurator", "claude_code", "t",
                               {"user_id": 1}, topic_id=333)
        # ACTIVE (default) → undergoing; empty open_questions at snapshot time.

        captured: dict = {}

        def _spawn(r, *, reconcile_snapshot=None, reconcile_claimed=None,
                   telegram_ready=None):
            captured["snapshot"] = reconcile_snapshot

        async def _fake_start(*, engagement_id):
            # The resumed CLI registers a FRESH ask AFTER the pre-service snapshot
            # but BEFORE _spawn_background_tasks (attach) runs.
            nn = await reg.allocate_question_number(engagement_id)
            await reg.add_open_question(engagement_id, nn, 800, text="Q: fresh?",
                                        kind="anchor")

        monkeypatch.setattr(s6_rc, "start_service", _fake_start)

        driver = AsyncMock()
        driver._spawn_background_tasks = _spawn
        driver.adopt_summary_if_missing = AsyncMock()
        # schedule_boot_reconcile is SYNC in production (returns a task/None); the
        # casa_core refused-pass calls it un-awaited, so keep it a sync mock.
        driver.schedule_boot_reconcile = MagicMock(return_value=None)

        await casa_core.replay_undergoing_engagements(
            registry=reg, driver=driver, executor_registry=None,
            engagements_root=str(tmp_path / "eng"), telegram_ready=None)

        # B3: the empty-oq record was snapshotted as [] (NOT None) — the fresh ask
        # created between snapshot and attach is NOT in the reconcile set.
        assert captured.get("snapshot") == []
        # And the fresh question survives in the ledger (never expired).
        assert reg.open_question_entries(rec.id)
