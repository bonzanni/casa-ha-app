"""A2b — verified group-wide ``force_turn_boundary`` backstop (v0.83.0).

On the 2nd ``operator_away`` refusal in an away-episode the driver force-ends
the CLI turn so a doctrine-defying agent cannot loop ask→refusal at token
speed. The kill is a VERIFIED, GROUP-WIDE ladder in ``s6_rc``:

  * STRICT tri-state entry probe (down / up / unknown) — ``unknown`` never
    reads as success ("not suspending blind");
  * ``os.killpg(pid, SIGTERM)`` on the whole group (leader + MCP/tool children);
  * bounded verification of GROUP EXTINCTION (``os.killpg(pgid, 0)`` →
    ``ProcessLookupError``), NOT leader turnover;
  * timeout → ``os.killpg(RECORDED pgid, SIGKILL)`` → re-poll the same probe;
  * truthful bool — a False is WARN-logged, never falsely reported suspended.

Fakes inject the probe / pid / killpg / sleep seams; no real signals, no real
s6. The ladder tests assert EXACT call sequences so a mis-ordered ladder fails
meaningfully.
"""

from __future__ import annotations

import logging
import signal
import types

import pytest

from drivers import s6_rc

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers: install the injectable seams on the s6_rc module
# ---------------------------------------------------------------------------


def _install(monkeypatch, *, probe, pid=4242, killpg):
    async def _fake_probe(scandir):
        return probe() if callable(probe) else probe

    async def _fake_pid(*, engagement_id):
        return pid

    monkeypatch.setattr(s6_rc, "_probe_service_down", _fake_probe)
    monkeypatch.setattr(s6_rc, "service_pid", _fake_pid)
    monkeypatch.setattr(s6_rc, "_killpg", killpg)


async def _no_sleep_recorder():
    calls: list[float] = []

    async def _sleep(d):
        calls.append(d)

    return calls, _sleep


# ---------------------------------------------------------------------------
# 1. unknown entry probe → False, NO signals, WARN
# ---------------------------------------------------------------------------


async def test_unknown_probe_returns_false_no_signals(monkeypatch, caplog):
    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))

    _install(monkeypatch, probe="unknown", killpg=killpg)
    with caplog.at_level(logging.WARNING, logger="drivers.s6_rc"):
        result = await s6_rc.force_turn_boundary(engagement_id="e1")

    assert result is False
    assert seq == []
    assert any("not suspending blind" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 2. down entry probe → True immediately, NO signals
# ---------------------------------------------------------------------------


async def test_down_probe_returns_true_no_signals(monkeypatch):
    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))

    _install(monkeypatch, probe="down", killpg=killpg)
    result = await s6_rc.force_turn_boundary(engagement_id="e1")

    assert result is True
    assert seq == []


# ---------------------------------------------------------------------------
# 3. up → SIGTERM group → extinction on poll N → True; exact sequence; no KILL
# ---------------------------------------------------------------------------


async def test_up_sigterm_then_extinct_no_kill(monkeypatch):
    seq: list[tuple] = []
    state = {"zero": 0}

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        if sig == 0:
            state["zero"] += 1
            if state["zero"] >= 3:      # extinct on the 3rd emptiness probe
                raise ProcessLookupError

    _install(monkeypatch, probe="up", pid=777, killpg=killpg)
    calls, sleep = await _no_sleep_recorder()
    result = await s6_rc.force_turn_boundary(engagement_id="e1", sleep=sleep)

    assert result is True
    # SIGTERM to the group first, then emptiness probes on the RECORDED pgid.
    assert seq[0] == (777, signal.SIGTERM)
    assert seq[1:] == [(777, 0), (777, 0), (777, 0)]
    assert all(sig != signal.SIGKILL for _, sig in seq)


# ---------------------------------------------------------------------------
# 4. SIGTERM ignored → SIGKILL to the RECORDED pgid → extinct → True
# ---------------------------------------------------------------------------


async def test_sigterm_ignored_escalates_to_sigkill(monkeypatch):
    seq: list[tuple] = []
    state = {"killed": False}

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        if sig == signal.SIGKILL:
            state["killed"] = True
            return
        if sig == 0 and state["killed"]:
            raise ProcessLookupError
        # SIGTERM, or emptiness-probe before the kill → still alive.

    _install(monkeypatch, probe="up", pid=555, killpg=killpg)
    calls, sleep = await _no_sleep_recorder()
    result = await s6_rc.force_turn_boundary(engagement_id="e1", sleep=sleep)

    assert result is True
    signals = [sig for _, sig in seq]
    # SIGTERM once, then 20 alive emptiness probes, then SIGKILL, then extinct.
    assert seq[0] == (555, signal.SIGTERM)
    assert signals.count(0) == 20 + 1          # 20 pre-kill alive + 1 post-kill
    assert (555, signal.SIGKILL) in seq        # kill on the RECORDED pgid
    # every emptiness probe used the recorded pgid, never a re-read pid.
    assert all(pgid == 555 for pgid, _ in seq)


# ---------------------------------------------------------------------------
# 5. still alive after SIGKILL re-poll → truthful False + WARN
# ---------------------------------------------------------------------------


async def test_never_extinct_returns_false(monkeypatch, caplog):
    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        # never raises ProcessLookupError → group refuses to die.

    _install(monkeypatch, probe="up", pid=999, killpg=killpg)
    calls, sleep = await _no_sleep_recorder()
    with caplog.at_level(logging.WARNING, logger="drivers.s6_rc"):
        result = await s6_rc.force_turn_boundary(engagement_id="e1", sleep=sleep)

    assert result is False
    assert (999, signal.SIGKILL) in seq
    assert any("NOT verified" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 6. ProcessLookupError at the FIRST killpg (SIGTERM) → already extinct → True
# ---------------------------------------------------------------------------


async def test_processlookup_on_sigterm_is_extinct(monkeypatch):
    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        if sig == signal.SIGTERM:
            raise ProcessLookupError

    _install(monkeypatch, probe="up", pid=111, killpg=killpg)
    result = await s6_rc.force_turn_boundary(engagement_id="e1")

    assert result is True
    # SIGTERM raised extinct → no emptiness probing needed.
    assert seq == [(111, signal.SIGTERM)]


# ---------------------------------------------------------------------------
# Driver-side trigger (record_away_refusal → force-end once per episode)
# ---------------------------------------------------------------------------


def _driver(tmp_path):
    from unittest.mock import AsyncMock

    from drivers.claude_code_driver import ClaudeCodeDriver

    return ClaudeCodeDriver(
        engagements_root=str(tmp_path), send_to_topic=AsyncMock(),
        casa_framework_mcp_url="x")


async def _drain():
    # let the create_task'd force-end coroutine run.
    import asyncio
    for _ in range(5):
        await asyncio.sleep(0)


async def test_second_away_refusal_fires_force_end_once(tmp_path):
    from unittest.mock import AsyncMock

    eid = "eng00000000000001"
    drv = _driver(tmp_path)
    fake = AsyncMock(return_value=True)
    drv._force_turn_boundary = fake
    drv._operator_away[eid] = True
    drv._epoch_pending[eid] = 7

    assert drv.record_away_refusal(eid) == 1
    await _drain()
    fake.assert_not_awaited()

    assert drv.record_away_refusal(eid) == 2
    await _drain()
    fake.assert_awaited_once_with(engagement_id=eid)
    # the current epoch was marked expected-terminated BEFORE signalling.
    assert drv._forced_suspend_epochs[eid] == 7

    # 3rd refusal in the SAME episode does NOT re-fire.
    assert drv.record_away_refusal(eid) == 3
    await _drain()
    fake.assert_awaited_once()


async def test_new_episode_can_fire_again_after_clear(tmp_path):
    from unittest.mock import AsyncMock

    eid = "eng00000000000002"
    drv = _driver(tmp_path)
    fake = AsyncMock(return_value=True)
    drv._force_turn_boundary = fake
    drv._operator_away[eid] = True

    drv.record_away_refusal(eid)
    drv.record_away_refusal(eid)
    await _drain()
    assert fake.await_count == 1

    # operator returns → away clears (resets the fired flag + counter).
    await drv._clear_operator_away(eid)

    # a NEW away-episode.
    drv._operator_away[eid] = True
    drv.record_away_refusal(eid)
    drv.record_away_refusal(eid)
    await _drain()
    assert fake.await_count == 2


async def test_abnormal_exit_log_annotated_for_marked_epoch(tmp_path, caplog):
    eid = "eng00000000000003"
    drv = _driver(tmp_path)
    engagement = types.SimpleNamespace(id=eid)

    drv._forced_suspend_epochs[eid] = 42
    with caplog.at_level(logging.INFO, logger="drivers.claude_code_driver"):
        drv._log_abnormal_exit(engagement, 42)
    assert any(
        "forced suspend (operator away)" in r.message
        and r.levelno == logging.INFO
        for r in caplog.records
    )
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


async def test_abnormal_exit_unmarked_epoch_keeps_warn(tmp_path, caplog):
    eid = "eng00000000000004"
    drv = _driver(tmp_path)
    engagement = types.SimpleNamespace(id=eid)

    # a marked DIFFERENT epoch must not annotate this one.
    drv._forced_suspend_epochs[eid] = 42
    with caplog.at_level(logging.INFO, logger="drivers.claude_code_driver"):
        drv._log_abnormal_exit(engagement, 99)
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert not any(
        "forced suspend" in r.message for r in caplog.records
    )
