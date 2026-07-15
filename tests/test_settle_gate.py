"""v0.81.0 W-R1 (Sol r2-2) — confirmed-edit settle gating helper.

``confirmed_settle_edit`` bounds-retries a settle edit and reports whether the
edit was CONFIRMED, so the three close paths (finish hook, boot reconciliation,
anchor settlement) close the durable open-question ledger entry ONLY after a
confirmed successful edit. Exactly 3 attempts, 0.5s → 1s → 2s backoff, injected
clock (never patches ``<module>.asyncio.sleep``).
"""

from __future__ import annotations

import pytest

from settle_gate import SETTLE_BACKOFFS, confirmed_settle_edit

pytestmark = pytest.mark.asyncio


async def test_backoff_schedule_is_half_one_two():
    assert SETTLE_BACKOFFS == (0.5, 1.0, 2.0)


async def test_all_failures_three_attempts_full_backoff_returns_false():
    attempts = {"n": 0}
    sleeps: list[float] = []

    async def _sleep(d):
        sleeps.append(d)

    async def _edit():
        attempts["n"] += 1
        return False

    ok = await confirmed_settle_edit(_edit, sleep=_sleep)
    assert ok is False
    assert attempts["n"] == 3
    assert sleeps == [0.5, 1.0, 2.0]


async def test_success_on_second_attempt_stops_and_confirms():
    attempts = {"n": 0}
    sleeps: list[float] = []

    async def _sleep(d):
        sleeps.append(d)

    async def _edit():
        attempts["n"] += 1
        return attempts["n"] == 2  # fail 1, succeed 2

    ok = await confirmed_settle_edit(_edit, sleep=_sleep)
    assert ok is True
    assert attempts["n"] == 2
    assert sleeps == [0.5]  # slept once after the first failure, then confirmed


async def test_first_attempt_success_no_sleep():
    sleeps: list[float] = []

    async def _sleep(d):
        sleeps.append(d)

    async def _edit():
        return True

    ok = await confirmed_settle_edit(_edit, sleep=_sleep)
    assert ok is True
    assert sleeps == []


async def test_raising_edit_counts_as_failed_attempt():
    attempts = {"n": 0}
    sleeps: list[float] = []

    async def _sleep(d):
        sleeps.append(d)

    async def _edit():
        attempts["n"] += 1
        raise RuntimeError("transient")

    ok = await confirmed_settle_edit(_edit, sleep=_sleep)
    assert ok is False
    assert attempts["n"] == 3
    assert sleeps == [0.5, 1.0, 2.0]
