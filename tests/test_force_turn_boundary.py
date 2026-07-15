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

Fakes inject the probe / pid / getpgid / killpg / sleep seams; no real signals,
no real s6. The ladder tests assert EXACT call sequences so a mis-ordered ladder
fails meaningfully. The recorded pgid comes from ``os.getpgid`` (NOT an assumed
pid — the run template never calls setsid), so a leader that is not its own
group leader still gets its whole group signalled on the RECORDED pgid.
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


def _install(monkeypatch, *, probe, pid=4242, killpg, getpgid=None):
    async def _fake_probe(scandir):
        # ONE atomic snapshot: status + pid together (Sol A2 review).
        status = probe() if callable(probe) else probe
        return status, pid

    def _fake_getpgid(p):
        # Default: the leader IS its own group leader (pgid == pid). Cases that
        # need a divergent group or a vanished leader pass an explicit callable.
        return p if getpgid is None else getpgid(p)

    monkeypatch.setattr(s6_rc, "_probe_status_and_pid", _fake_probe)
    monkeypatch.setattr(s6_rc, "_getpgid", _fake_getpgid)
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
# 7. leader NOT a group leader → all signals/probes use the RECORDED pgid
#    (os.getpgid), never the pid. Guards against the false verified-suspended:
#    killpg(pid) would raise ProcessLookupError → misread as "already extinct".
# ---------------------------------------------------------------------------


async def test_leader_not_group_leader_uses_recorded_pgid(monkeypatch):
    seq: list[tuple] = []
    state = {"zero": 0}

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        if sig == 0:
            state["zero"] += 1
            if state["zero"] >= 2:          # extinct on the 2nd emptiness probe
                raise ProcessLookupError

    # Leader pid 777, but its process GROUP is 700 (run template never setsid).
    _install(
        monkeypatch, probe="up", pid=777, killpg=killpg,
        getpgid=lambda p: 700,
    )
    calls, sleep = await _no_sleep_recorder()
    result = await s6_rc.force_turn_boundary(engagement_id="e1", sleep=sleep)

    assert result is True
    # SIGTERM first, then emptiness probes — ALL on the recorded pgid 700.
    assert seq[0] == (700, signal.SIGTERM)
    assert seq[1:] == [(700, 0), (700, 0)]
    # The pid 777 is NEVER signalled directly (that is the whole bug).
    assert all(pgid == 700 for pgid, _ in seq)


# ---------------------------------------------------------------------------
# 8. getpgid raises ProcessLookupError (leader vanished in the probe→getpgid
#    window) → cannot identify the group → truthful False, WARN, ZERO signals.
# ---------------------------------------------------------------------------


async def test_getpgid_vanished_returns_false_no_signals(monkeypatch, caplog):
    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))

    def _vanished(p):
        raise ProcessLookupError

    _install(
        monkeypatch, probe="up", pid=888, killpg=killpg, getpgid=_vanished,
    )
    with caplog.at_level(logging.WARNING, logger="drivers.s6_rc"):
        result = await s6_rc.force_turn_boundary(engagement_id="e1")

    assert result is False
    assert seq == []               # never guess a pgid → never signal
    assert any(
        "cannot verify suspension" in r.message for r in caplog.records
    )


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
    fake.assert_awaited_once()
    # the current epoch was marked expected-terminated BEFORE signalling AND is
    # passed to the kill as the expected-epoch guard, with the workspace dir.
    kwargs = fake.await_args.kwargs
    assert kwargs["engagement_id"] == eid
    assert kwargs["expected_epoch"] == 7
    assert kwargs["workspace_dir"].endswith(eid)
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


# ---------------------------------------------------------------------------
# Finding 4a: expected-epoch guard. The atomic status+pid probe closes the
# turn-ended/respawn race; if .spawn_epoch no longer equals the armed epoch the
# ladder aborts with False and ZERO signals (never kills the fresh spawn).
# ---------------------------------------------------------------------------


async def test_epoch_mismatch_aborts_with_no_signals(monkeypatch, tmp_path, caplog):
    import signal as _sig

    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))

    _install(monkeypatch, probe="up", pid=333, killpg=killpg)
    # The workspace epoch advanced past what the driver armed → turn respawned.
    (tmp_path / ".spawn_epoch").write_text("9\n")
    calls, sleep = await _no_sleep_recorder()

    with caplog.at_level(logging.INFO, logger="drivers.s6_rc"):
        result = await s6_rc.force_turn_boundary(
            engagement_id="e1", workspace_dir=str(tmp_path),
            expected_epoch=7, sleep=sleep)

    assert result is False
    assert seq == []              # ZERO signals — the fresh spawn is never killed
    assert _sig.SIGKILL not in [s for _, s in seq]
    assert any("turn already ended" in r.message for r in caplog.records)


async def test_epoch_match_proceeds_to_signal(monkeypatch, tmp_path):
    import signal as _sig

    seq: list[tuple] = []

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        if sig == _sig.SIGTERM:
            raise ProcessLookupError   # group already empty on the group signal

    _install(monkeypatch, probe="up", pid=333, killpg=killpg)
    (tmp_path / ".spawn_epoch").write_text("7\n")   # still the armed epoch

    result = await s6_rc.force_turn_boundary(
        engagement_id="e1", workspace_dir=str(tmp_path), expected_epoch=7)

    assert result is True
    assert seq == [(333, _sig.SIGTERM)]   # guard passed → signalled the group


# ---------------------------------------------------------------------------
# Finding 4b: away-clear cancels an in-flight force task; force_turn_boundary
# tolerates mid-poll cancellation cleanly (no SIGKILL after the cancel).
# ---------------------------------------------------------------------------


async def test_clear_operator_away_cancels_inflight_force_task(tmp_path):
    import asyncio

    eid = "eng00000000000005"
    drv = _driver(tmp_path)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking(**kwargs):
        started.set()
        await release.wait()      # simulate a long group-extinction verify
        return True

    drv._force_turn_boundary = _blocking
    drv._operator_away[eid] = True

    drv.record_away_refusal(eid)
    drv.record_away_refusal(eid)      # 2nd refusal fires the force task
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task = drv._force_tasks[eid]
    assert not task.done()

    # Operator returns → away clears → the in-flight kill is cancelled.
    await drv._clear_operator_away(eid)
    await _drain()

    assert task.cancelled()
    assert eid not in drv._force_tasks


async def test_force_turn_boundary_tolerates_cancellation_mid_poll(monkeypatch):
    import asyncio
    import signal as _sig

    seq: list[tuple] = []
    block = asyncio.Event()

    def killpg(pgid, sig):
        seq.append((pgid, sig))
        # emptiness probes always report alive (never raise) → poll would spin.

    async def _sleep(_d):
        await block.wait()        # wedge the poll between probes

    _install(monkeypatch, probe="up", pid=444, killpg=killpg)
    task = asyncio.ensure_future(
        s6_rc.force_turn_boundary(engagement_id="e1", sleep=_sleep))
    for _ in range(6):            # let it send SIGTERM + one probe + enter sleep
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # SIGTERM was delivered; cancellation stopped the poll BEFORE any SIGKILL.
    assert (444, _sig.SIGTERM) in seq
    assert _sig.SIGKILL not in [s for _, s in seq]
