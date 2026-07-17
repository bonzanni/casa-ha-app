"""Task 6 / v0.83.0 §A3 — the open-questions ledger ``answered``/``stale_mids``
split + unanswered-only accessors + the entry-removal invariant.

REAL registry over a tmp tombstone (never mocked persistence except to inject a
strict-write failure), REAL SummaryController + REAL OutputSequencer with fake
wire fns for the summary-consumer test, injected clocks. Never patches
``<module>.asyncio.sleep`` (the shared attribute — the memory-cage rule)."""

from __future__ import annotations

import asyncio
import json
import unittest.mock as _mock
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _make_registry(tmp_path: Path):
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t", {}, topic_id=999)
    return reg, rec


async def _add_q(reg, rec, *, kind: str, mid, text: str):
    n = await reg.allocate_question_number(rec.id)
    await reg.add_open_question(rec.id, n, mid, text=text, kind=kind)
    return n


def _make_driver(tmp_path: Path, reg, *, edit_topic_message=None):
    from drivers.claude_code_driver import ClaudeCodeDriver

    return ClaudeCodeDriver(
        engagements_root=str(tmp_path / "engagements"),
        send_to_topic=AsyncMock(),
        casa_framework_mcp_url="http://x",
        edit_topic_message=edit_topic_message,
        registry=reg,
    )


async def _park(_dt: float) -> None:
    await asyncio.Event().wait()


# ---------------------------------------------------------------------------
# 1. accessor split — answered invisible to gates, raw iterates it
# ---------------------------------------------------------------------------


class TestAccessorSplit:
    async def test_answered_excluded_but_raw_iterates(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="button", mid=7001, text="Q1: a?")
        n2 = await _add_q(reg, rec, kind="anchor", mid=7002, text="Q2: b?")

        assert reg.open_question_numbers(rec.id) == [n1, n2]
        assert reg.oldest_open_anchor(rec.id)["n"] == n2

        assert await reg.mark_question_answered(rec.id, n2) is True

        # Answered n2 disappears from the summary/gate accessors ...
        assert reg.open_question_numbers(rec.id) == [n1]
        assert reg.oldest_open_anchor(rec.id) is None  # only answered anchor
        # ... but the RAW view still iterates it (reconcile/settle need it).
        raw = reg.open_question_entries(rec.id)
        assert {q["n"] for q in raw} == {n1, n2}
        assert next(q for q in raw if q["n"] == n2)["answered"] is True

    async def test_mark_question_answered_unknown_returns_false(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        assert await reg.mark_question_answered(rec.id, 999) is False
        assert await reg.mark_question_answered("nope", 1) is False

    async def test_mark_answered_persists_across_reload(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=7001, text="Q1: a?")
        await reg.mark_question_answered(rec.id, n1)

        reg2 = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        assert reg2.open_question_numbers(rec.id) == []  # answered → invisible
        assert reg2.open_question_entries(rec.id)[0]["answered"] is True


# ---------------------------------------------------------------------------
# 2. pre-v0.83 record load tolerance (entries without the new fields)
# ---------------------------------------------------------------------------


class TestLegacyLoadTolerance:
    async def test_entry_without_new_fields_loads_and_mutates(self, tmp_path):
        from engagement_registry import EngagementRegistry

        # A pre-v0.83 tombstone: open_questions entries have NO answered/stale_mids.
        legacy = [{
            "id": "eLEG", "kind": "executor", "role_or_type": "configurator",
            "driver": "claude_code", "status": "idle", "topic_id": 5,
            "started_at": 1.0, "last_user_turn_ts": 1.0,
            "next_question_number": 3,
            "open_questions": [
                {"n": 1, "tg_message_id": 100, "kind": "anchor", "text": "Q1"},
                {"n": 2, "tg_message_id": 200, "kind": "button", "text": "Q2"},
            ],
        }]
        (tmp_path / "e.json").write_text(json.dumps(legacy), encoding="utf-8")

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg.load()
        # Absent-tolerant: both treated as unanswered.
        assert reg.open_question_numbers("eLEG") == [1, 2]
        assert reg.oldest_open_anchor("eLEG")["n"] == 1
        # And the new mutators work on a legacy entry.
        assert await reg.mark_question_answered("eLEG", 1) is True
        assert await reg.stage_stale_mid("eLEG", 2, 999) is True
        assert reg.open_question_numbers("eLEG") == [2]
        entry2 = next(q for q in reg.open_question_entries("eLEG") if q["n"] == 2)
        # v0.84.0 (round-4 §D6): defaults to kind="plain" (today's rendering).
        assert entry2["stale_mids"] == [{"mid": 999, "kind": "plain"}]


# ---------------------------------------------------------------------------
# 2b. D6 — legacy bare-int ``stale_mids`` (pre-round-4 records that ALREADY
# had a staged re-anchor in flight) settle with TODAY'S full-body rendering,
# never the new marker-only text — normalize_stale_mid_entry defaults an
# unrecognized (bare-int) entry to kind="plain".
# ---------------------------------------------------------------------------


class TestLegacyBareIntStaleMidSettle:
    async def test_legacy_bare_int_stale_mid_settles_full_body(self, tmp_path):
        from engagement_registry import EngagementRegistry

        legacy = [{
            "id": "eLEG2", "kind": "executor", "role_or_type": "configurator",
            "driver": "claude_code", "status": "idle", "topic_id": 5,
            "started_at": 1.0, "last_user_turn_ts": 1.0,
            "next_question_number": 2,
            "open_questions": [
                {
                    "n": 1, "tg_message_id": 100, "kind": "anchor",
                    "text": "Q1: A?", "answered": False,
                    # Pre-round-4 shape: a bare int, not {"mid","kind"}.
                    "stale_mids": [999],
                },
            ],
        }]
        (tmp_path / "e.json").write_text(json.dumps(legacy), encoding="utf-8")

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg.load()
        rec = reg._records["eLEG2"]

        edits: list[tuple[int, str]] = []

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            edits.append((mid, text))
            return True

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        drv._sleep = AsyncMock()

        await drv.reconcile_open_questions(rec)

        stale_text = next(text for mid, text in edits if mid == 999)
        # Legacy bare-int normalizes to kind="plain" — TODAY'S full-body +
        # suffix rendering, never the marker-only text.
        assert stale_text == "Q1: A?\n⌛ expired — answer by text below"
        assert "MOVED" not in stale_text
        assert reg.open_question_entries("eLEG2") == []


# ---------------------------------------------------------------------------
# 3. stale_mids mutators — strict, idempotent, persisted
# ---------------------------------------------------------------------------


class TestStaleMidMutators:
    async def test_stage_unstage_roundtrip(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=7001, text="Q1")
        assert await reg.stage_stale_mid(rec.id, n1, 5001) is True
        assert await reg.stage_stale_mid(rec.id, n1, 5001) is True  # idempotent
        assert await reg.stage_stale_mid(rec.id, n1, 5002) is True
        entry = reg.open_question_entries(rec.id)[0]
        assert entry["stale_mids"] == [
            {"mid": 5001, "kind": "plain"}, {"mid": 5002, "kind": "plain"},
        ]

        # Persisted across reload.
        reg2 = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        assert reg2.open_question_entries(rec.id)[0]["stale_mids"] == [
            {"mid": 5001, "kind": "plain"}, {"mid": 5002, "kind": "plain"},
        ]

        assert await reg2.unstage_stale_mid(rec.id, n1, 5001) is True
        assert await reg2.unstage_stale_mid(rec.id, n1, 5001) is True  # no-op ok
        assert reg2.open_question_entries(rec.id)[0]["stale_mids"] == [
            {"mid": 5002, "kind": "plain"},
        ]

    async def test_stage_unknown_returns_false(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        assert await reg.stage_stale_mid(rec.id, 99, 1) is False
        assert await reg.unstage_stale_mid("nope", 1, 1) is False

    async def test_mark_answered_strict_rolls_back_and_raises(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=7001, text="Q1")
        with _mock.patch.object(
            reg, "_write_tombstone", side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError):
                await reg.mark_question_answered(rec.id, n1)
        # Rolled back — the flag never half-landed.
        assert reg.open_question_entries(rec.id)[0].get("answered") is False
        assert reg.open_question_numbers(rec.id) == [n1]


# ---------------------------------------------------------------------------
# 3b. D2 (round-4 §D6, Sol r3-3/r17) — update_question_mid's atomic kind flip.
# The re-anchor pass stages the OLD mid as "plain", then update_question_mid
# must, in ONE strict transaction, persist tg_message_id=new_mid AND flip
# THAT question's staged old-mid entry (mid == the previous tg_message_id)
# from "plain" to "reanchored". No intermediate durable state.
# ---------------------------------------------------------------------------


class TestUpdateQuestionMidKindFlip:
    async def test_transaction_persists_new_mid_and_flips_kind_atomically(
        self, tmp_path,
    ):
        from engagement_registry import EngagementRegistry

        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=7001, text="Q1")
        assert await reg.stage_stale_mid(rec.id, n1, 7001, kind="plain") is True

        with _mock.patch.object(
            reg, "_write_tombstone", wraps=reg._write_tombstone,
        ) as spy:
            assert await reg.update_question_mid(rec.id, n1, 8002) is True
            # One strict transaction == exactly one tombstone write for this
            # call — the mid persist and the kind flip never land as two
            # separate writes (which would expose an intermediate state).
            assert spy.call_count == 1

        entry = reg.open_question_entries(rec.id)[0]
        assert entry["tg_message_id"] == 8002
        assert entry["stale_mids"] == [{"mid": 7001, "kind": "reanchored"}]

        # Reload from disk — both changes landed in the SAME persisted write,
        # with no observable intermediate ("new mid, still plain" or "old
        # mid, already reanchored") state.
        reg2 = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        entry2 = reg2.open_question_entries(rec.id)[0]
        assert entry2["tg_message_id"] == 8002
        assert entry2["stale_mids"] == [{"mid": 7001, "kind": "reanchored"}]

    async def test_flip_targets_only_this_questions_staged_old_mid(
        self, tmp_path,
    ):
        # A second, unrelated "plain" stale entry on the SAME question (e.g.
        # from an earlier expiry) must NOT be flipped — only the entry whose
        # mid equals the old tg_message_id being replaced.
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=7001, text="Q1")
        assert await reg.stage_stale_mid(rec.id, n1, 9999, kind="plain") is True
        assert await reg.stage_stale_mid(rec.id, n1, 7001, kind="plain") is True

        assert await reg.update_question_mid(rec.id, n1, 8002) is True

        entry = reg.open_question_entries(rec.id)[0]
        stale_by_mid = {s["mid"]: s["kind"] for s in entry["stale_mids"]}
        assert stale_by_mid[7001] == "reanchored"  # the just-replaced old mid
        assert stale_by_mid[9999] == "plain"        # untouched

    async def test_staged_then_stopped_before_update_leaves_plain(
        self, tmp_path,
    ):
        # Simulated crash BEFORE update_question_mid runs: staging landed,
        # the transaction that would flip the kind + persist the new mid
        # never ran. Old mid stays current; the staged entry stays "plain"
        # so it settles full-body (never a marker for a copy that never
        # existed).
        from engagement_registry import EngagementRegistry

        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=7001, text="Q1")
        assert await reg.stage_stale_mid(rec.id, n1, 7001, kind="plain") is True
        # update_question_mid deliberately NOT called here.

        entry = reg.open_question_entries(rec.id)[0]
        assert entry["tg_message_id"] == 7001
        assert entry["stale_mids"] == [{"mid": 7001, "kind": "plain"}]

        reg2 = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        entry2 = reg2.open_question_entries(rec.id)[0]
        assert entry2["tg_message_id"] == 7001
        assert entry2["stale_mids"] == [{"mid": 7001, "kind": "plain"}]

    async def test_failed_commit_rolls_back_both_mid_and_kind_in_memory(
        self, tmp_path,
    ):
        from engagement_registry import EngagementRegistry

        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=7001, text="Q1")
        assert await reg.stage_stale_mid(rec.id, n1, 7001, kind="plain") is True

        with _mock.patch.object(
            reg, "_write_tombstone", side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError):
                await reg.update_question_mid(rec.id, n1, 8002)

        # BOTH the mid update and the kind flip rolled back in memory — not
        # just one of the two fields.
        entry = reg.open_question_entries(rec.id)[0]
        assert entry["tg_message_id"] == 7001
        assert entry["stale_mids"] == [{"mid": 7001, "kind": "plain"}]

        # A subsequent successful commit (no failure injected) persists the
        # ORIGINAL pre-failure state faithfully, then applies the real flip.
        assert await reg.update_question_mid(rec.id, n1, 8002) is True
        reg2 = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        entry2 = reg2.open_question_entries(rec.id)[0]
        assert entry2["tg_message_id"] == 8002
        assert entry2["stale_mids"] == [{"mid": 7001, "kind": "reanchored"}]

    async def test_unstage_persist_failure_leaves_reanchored_intact(
        self, tmp_path,
    ):
        # After a successful flip, a LATER failed unstage (the confirmed-edit
        # settle's cleanup persist) must not resurrect "plain" — the
        # "reanchored" entry stays intact for an idempotent re-settle.
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=7001, text="Q1")
        assert await reg.stage_stale_mid(rec.id, n1, 7001, kind="plain") is True
        assert await reg.update_question_mid(rec.id, n1, 8002) is True
        entry = reg.open_question_entries(rec.id)[0]
        assert entry["stale_mids"] == [{"mid": 7001, "kind": "reanchored"}]

        with _mock.patch.object(
            reg, "_write_tombstone", side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError):
                await reg.unstage_stale_mid(rec.id, n1, 7001)

        # Rolled back — the reanchored entry is still there, unchanged.
        entry = reg.open_question_entries(rec.id)[0]
        assert entry["stale_mids"] == [{"mid": 7001, "kind": "reanchored"}]

        # Idempotent re-settle: a later successful unstage removes it cleanly.
        assert await reg.unstage_stale_mid(rec.id, n1, 7001) is True
        assert reg.open_question_entries(rec.id)[0]["stale_mids"] == []


# ---------------------------------------------------------------------------
# 4. REAL SummaryController consumer — answered stops the ⏳/open-questions line
# ---------------------------------------------------------------------------


class TestSummaryConsumer:
    async def test_answered_unconfirmed_settle_clears_summary_and_status(
        self, tmp_path,
    ):
        from channels.output_sequencer import OutputSequencer
        from drivers.summary_controller import SummaryController, STATUS_WORKING

        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=7001, text="Q1: DB?")

        drv = _make_driver(tmp_path, reg)
        eid = rec.id

        edits: list[tuple[int, str]] = []

        async def _send(topic_id, text):
            return 500

        async def _edit(topic_id, mid, text):
            edits.append((mid, text))
            return True

        seq = OutputSequencer(
            engagement_id=eid, topic_id=999,
            send_message=_send, edit_message=_edit)
        ctrl = SummaryController(
            engagement_id=eid, sequencer=seq, goal_line="goal",
            open_question_numbers=lambda: drv._effective_open_question_numbers(eid),
            message_id=500, _sleep=_park,
        )
        drv._summaries[eid] = ctrl
        drv._turn_running[eid] = True

        # Before the answer: recompute lands ⏳ waiting, the line shows Q1.
        await drv.recompute_engagement_status(eid)
        assert edits[-1][1].splitlines()[0] == "⏳ waiting for your reply"
        assert f"Open questions: Q{n1}" in edits[-1][1]

        # The answer lands (lifecycle) but its visual settle is NOT yet confirmed
        # — the entry stays in the ledger, only the answered flag flips.
        await reg.mark_question_answered(eid, n1)
        await drv.recompute_engagement_status(eid)

        # The summary drops the open-questions line and returns to ⚙️ working —
        # NOT stuck ⏳ waiting on an already-answered question.
        assert "Open questions" not in edits[-1][1]
        assert edits[-1][1].splitlines()[0] == STATUS_WORKING
        ctrl.shutdown()


# ---------------------------------------------------------------------------
# 5. entry-removal invariant (via _settle_open_anchor)
# ---------------------------------------------------------------------------


class TestEntryRemovalInvariant:
    async def test_unconfirmed_current_settle_retains_entry(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=8001, text="Q1: A?")

        sleeps: list[float] = []

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            return False  # transient failure — never confirmed

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        drv._sleep = lambda d: sleeps.append(d) or asyncio.sleep(0)

        amid = await drv._settle_open_anchor(rec, operator_msg_id=42)
        assert amid == 8001                       # still threads
        assert reg.open_question_entries(rec.id)  # entry RETAINED
        assert sleeps == [0.5, 1.0, 2.0]          # bounded retry

    async def test_confirmed_current_nonempty_stale_retains_and_settles_stale(
        self, tmp_path,
    ):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=8001, text="Q1: A?")
        await reg.stage_stale_mid(rec.id, n1, 9001)

        settled: list[int] = []

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            settled.append(mid)
            # current (8001) confirms; the stale copy (9001) never does.
            return mid == 8001

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        drv._sleep = AsyncMock()

        await drv._settle_open_anchor(rec, operator_msg_id=42)
        # BOTH the current copy AND the stale copy were settle-attempted.
        assert 8001 in settled and 9001 in settled
        # Entry RETAINED (stale not confirmed) with the stale mid still staged.
        entry = reg.open_question_entries(rec.id)[0]
        assert entry["stale_mids"] == [{"mid": 9001, "kind": "plain"}]

    async def test_confirmed_current_emptied_stale_removes_entry(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=8001, text="Q1: A?")
        await reg.stage_stale_mid(rec.id, n1, 9001)

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            return True  # every copy confirms

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        drv._sleep = AsyncMock()

        await drv._settle_open_anchor(rec, operator_msg_id=42)
        # Current confirmed AND stale emptied → entry REMOVED.
        assert reg.open_question_entries(rec.id) == []


# ---------------------------------------------------------------------------
# 6. stale_mids settled by boot reconciliation (per-mid confirmed gate)
# ---------------------------------------------------------------------------


class TestReconcileStaleMids:
    async def test_reconcile_settles_stale_unconfirmed_retains(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="button", mid=7001, text="Q1: A?")
        await reg.stage_stale_mid(rec.id, n1, 9001)
        await reg.stage_stale_mid(rec.id, n1, 9002)

        settled: list[int] = []

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            settled.append(mid)
            # current + 9001 confirm; 9002 stays unconfirmed.
            return mid != 9002

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        drv._sleep = AsyncMock()

        await drv.reconcile_open_questions(rec)
        # Confirmed copies (7001, 9001) attempted + 9002 attempted.
        assert {7001, 9001, 9002} <= set(settled)
        # Entry RETAINED because 9002 unconfirmed; 9001 un-staged, 9002 kept.
        entry = reg.open_question_entries(rec.id)[0]
        assert entry["stale_mids"] == [{"mid": 9002, "kind": "plain"}]

    async def test_reconcile_answered_entry_uses_answered_copy(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=7001, text="Q1: A?")
        await reg.mark_question_answered(rec.id, n1)

        edits: list[tuple[int, str]] = []

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            edits.append((mid, text))
            return True

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        drv._sleep = AsyncMock()

        await drv.reconcile_open_questions(rec)
        # An answered entry settles with the ✅ copy (not ⌛).
        assert edits[-1][1].endswith("✅ answered below")
        assert reg.open_question_entries(rec.id) == []  # confirmed → removed


# ---------------------------------------------------------------------------
# 6b. D6 — a ``stale_mids`` entry recorded kind="reanchored" settles to the
# EXACT pinned marker-only text (open/terminal), NEVER the duplicated body.
# ---------------------------------------------------------------------------


class TestReanchoredKindSettle:
    async def test_reanchored_stale_settles_exact_terminal_marker(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=8001, text="Q1: A?")
        await reg.stage_stale_mid(rec.id, n1, 9001, kind="reanchored")

        edits: list[tuple[int, str]] = []

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            edits.append((mid, text))
            return True

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        drv._sleep = AsyncMock()

        await drv._settle_open_anchor(rec, operator_msg_id=42)

        stale_text = next(text for mid, text in edits if mid == 9001)
        assert stale_text == f"⤵ MOVED Q{n1} — resolved below"
        assert "Q1: A?" not in stale_text        # never the duplicated body
        assert reg.open_question_entries(rec.id) == []  # both confirmed → closed

    async def test_reanchored_stale_unconfirmed_retains_kind(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=8001, text="Q1: A?")
        await reg.stage_stale_mid(rec.id, n1, 9001, kind="reanchored")

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            return mid != 9001   # current confirms; the reanchored stale doesn't

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        drv._sleep = AsyncMock()

        await drv._settle_open_anchor(rec, operator_msg_id=42)

        entry = reg.open_question_entries(rec.id)[0]
        assert entry["stale_mids"] == [{"mid": 9001, "kind": "reanchored"}]

    async def test_boot_reconcile_retained_reanchored_marker_only(self, tmp_path):
        """(d) A restart's fresh registry load, reconciling a PRE-EXISTING
        ``reanchored`` stale entry (staged before the crash) — the boot
        reconcile owner must render marker-only, never the duplicated body."""
        from engagement_registry import EngagementRegistry

        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=8001, text="Q1: A?")
        await reg.stage_stale_mid(rec.id, n1, 9001, kind="reanchored")

        reg2 = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        rec2 = reg2._records[rec.id]

        edits: list[tuple[int, str]] = []

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            edits.append((mid, text))
            return True

        drv2 = _make_driver(tmp_path, reg2, edit_topic_message=_edit)
        drv2._sleep = AsyncMock()

        await drv2.reconcile_open_questions(rec2)

        stale_text = next(text for mid, text in edits if mid == 9001)
        assert stale_text == f"⤵ MOVED Q{n1} — resolved below"
        assert reg2.open_question_entries(rec.id) == []


# ---------------------------------------------------------------------------
# 7. answered-persist-failure policy — overlay honored, converges on retry
# ---------------------------------------------------------------------------


class TestPersistFailurePolicy:
    async def test_overlay_honored_then_flag_converges_on_retry(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=8001, text="Q1: A?")
        eid = rec.id

        # The unconfirmed settle edit keeps the entry present so we can observe
        # the flag converging independently of the visual settle.
        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            return False

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        drv._sleep = AsyncMock()

        # The answer lifecycle tries to persist answered — it RAISES.
        with _mock.patch.object(
            reg, "_write_tombstone", side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError):
                await reg.mark_question_answered(eid, n1)
        drv.mark_answered_overlay(eid, n1)

        # Overlay honored IMMEDIATELY by the union helper, even though the durable
        # flag never landed (reg still lists it as open).
        assert reg.open_question_numbers(eid) == [n1]
        assert drv._effective_open_question_numbers(eid) == []

        # A later settle attempt RETRIES the strict persist (now the disk is
        # healthy) → the flag becomes durable, surviving reload.
        await drv._settle_open_anchor(rec, operator_msg_id=42)
        entry = reg.open_question_entries(eid)[0]  # still present (edit failed)
        assert entry["answered"] is True
        assert reg.open_question_numbers(eid) == []

        from engagement_registry import EngagementRegistry
        reg2 = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()
        assert reg2.open_question_entries(eid)[0]["answered"] is True


# ---------------------------------------------------------------------------
# 8. single-crash residual — spooled answer + failed answered-persist + crash
# ---------------------------------------------------------------------------


class TestSingleCrashResidual:
    async def test_boot_reconcile_settles_expired_copy_spool_untouched(
        self, tmp_path,
    ):
        from engagement_registry import EngagementRegistry

        # Prior process: an anchor with a durably-spooled answer, but the
        # ``answered`` persist failed (flag stays False) and the process crashed
        # (the in-memory overlay is gone).
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=8001, text="Q1: DB?")
        eid = rec.id

        # A durable spool envelope for the answer (untouched by reconcile).
        spool = tmp_path / "spool.jsonl"
        spool.write_text(
            json.dumps({"text": "my answer", "tg_message_id": 42}) + "\n",
            encoding="utf-8")
        spool_before = spool.read_bytes()

        # "Crash": a fresh registry loaded from the same tombstone; fresh driver
        # with an EMPTY overlay.
        reg2 = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        await reg2.load()

        edits: list[tuple[int, str]] = []

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            edits.append((mid, text))
            return True

        drv2 = _make_driver(tmp_path, reg2, edit_topic_message=_edit)
        drv2._sleep = AsyncMock()

        rec2 = reg2._records[eid]
        await drv2.reconcile_open_questions(rec2)

        # The documented single-crash residual: boot settles the ⌛ EXPIRED copy
        # (wrong cosmetic copy — the answer will still be delivered next spawn),
        # and the entry is removed.
        assert edits[-1][1].endswith("⌛ expired — answer by text below")
        assert reg2.open_question_numbers(eid) == []
        assert reg2.open_question_entries(eid) == []
        # The spool is UNTOUCHED — the envelope remains deliverable.
        assert spool.read_bytes() == spool_before


# ---------------------------------------------------------------------------
# 9. A8 · Q1-settle observability — one INFO line per CONFIRMED settle
# ---------------------------------------------------------------------------


class TestSettleObservability:
    async def test_anchor_answer_logs_confirmed_answered(self, tmp_path, caplog):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="anchor", mid=8001, text="Q1: A?")

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            return True

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        drv._sleep = AsyncMock()

        with caplog.at_level("INFO"):
            await drv._settle_open_anchor(rec, operator_msg_id=42)

        line = next(m for m in caplog.messages if "ask settle CONFIRMED" in m)
        assert f"eng={rec.id[:8]}" in line
        assert f"q={n1}" in line
        assert "mid=8001" in line
        assert "outcome=answered" in line

    async def test_reconcile_expired_logs_confirmed_expired(self, tmp_path, caplog):
        reg, rec = await _make_registry(tmp_path)
        n1 = await _add_q(reg, rec, kind="button", mid=7001, text="Q1: A?")

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            return True

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        drv._sleep = AsyncMock()

        with caplog.at_level("INFO"):
            await drv.reconcile_open_questions(rec)

        line = next(m for m in caplog.messages if "ask settle CONFIRMED" in m)
        assert f"eng={rec.id[:8]}" in line
        assert f"q={n1}" in line
        assert "mid=7001" in line
        assert "outcome=expired" in line

    async def test_unconfirmed_settle_logs_nothing(self, tmp_path, caplog):
        reg, rec = await _make_registry(tmp_path)
        await _add_q(reg, rec, kind="anchor", mid=8001, text="Q1: A?")

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            return False  # never confirmed

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        drv._sleep = AsyncMock()

        with caplog.at_level("INFO"):
            await drv._settle_open_anchor(rec, operator_msg_id=42)

        assert not any("ask settle CONFIRMED" in m for m in caplog.messages)
