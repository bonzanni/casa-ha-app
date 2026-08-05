"""``event_spool.py`` — the ``/data/events`` spool protocol: emission,
casa-minted generations (reconstruct/repair/open), the conditional
delivery update, typed ack, sweep (watermark/valve/quarantine/tombstone/
removal), and the removal-record ledger.

Structure-mirrors ``tests/test_callback_spool.py`` where the protocols
overlap (fixtures, real-thread concurrency, ``os.utime``-driven TTL
determinism); the fold/generation machinery has no callback analogue and
is pinned fresh here against the reviewed spec's red-case list.
"""
import logging
import os
import threading
import time
from pathlib import Path

import pytest

import event_attempts as ea
import event_spool as es
from event_spool import (
    FOLD_BATCH_MAX,
    MAX_EMISSION_FILES,
    QUIESCENCE_S,
    TEMP_TTL_S,
    EventSpool,
    MarkerState,
    ROUTING_UNAVAILABLE,
)

E = "finance"              # emitter plugin name
EV = "invoice-created"     # event name
S1 = "reporting"           # subscriber plugin name
S2 = "audit"                # a second subscriber


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def spool(tmp_path):
    s = EventSpool(tmp_path / "events")
    s.ensure_emitter_dirs(E)
    try:
        yield s
    finally:
        s.close()


def _edir(spool, emitter=E) -> Path:
    return Path(spool.root) / emitter


def _emissions_dir(spool, emitter=E) -> Path:
    return _edir(spool, emitter) / "emissions"


def _state_dir(spool, emitter=E) -> Path:
    return _edir(spool, emitter) / "state"


def _delivery_dir(spool, emitter=E) -> Path:
    return _edir(spool, emitter) / "delivery"


def _utime(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def R(*subs, emitter=E, event=EV) -> dict:
    """A one-pair routed map: ``{(emitter, event): {subs...}}``."""
    return {(emitter, event): set(subs)}


def _emit(spool, *, emitter=E, event=EV, when=None) -> Path:
    p = es.emit(_edir(spool, emitter), event)
    if when is not None:
        _utime(p, when)
    return p


def _emit_n(spool, n, *, emitter=E, event=EV, start=1000.0, step=1.0) -> list:
    return [_emit(spool, emitter=emitter, event=event, when=start + i * step)
            for i in range(n)]


def _delivery_path(spool, subscriber, *, emitter=E, event=EV) -> Path:
    return _delivery_dir(spool, emitter) / f"{event}--{subscriber}.json"


def _read_delivery(spool, subscriber, *, emitter=E, event=EV) -> dict:
    return spool.read_delivery(emitter, event, subscriber)


def _read_state(spool, *, emitter=E, event=EV) -> dict:
    return spool.read_state(emitter, event)


def _write_raw(path: Path, text: str, when: "float | None" = None) -> Path:
    path.write_text(text)
    if when is not None:
        _utime(path, when)
    return path


# ---------------------------------------------------------------------------
# ensure_emitter_dirs
# ---------------------------------------------------------------------------


def test_ensure_emitter_dirs_creates_0770_tree(spool):
    base = _edir(spool)
    assert base.is_dir()
    for sub in ("emissions", "state", "delivery"):
        assert (base / sub).is_dir()
    assert (base / ".dir-id").is_file()
    assert oct(base.stat().st_mode)[-3:] == "770"


def test_ensure_emitter_dirs_is_idempotent(spool):
    tok1 = (_edir(spool) / ".dir-id").read_text()
    spool.ensure_emitter_dirs(E)
    tok2 = (_edir(spool) / ".dir-id").read_text()
    assert tok1 == tok2


def test_ensure_emitter_dirs_refuses_unsafe_name(spool):
    with pytest.raises(ValueError):
        spool.ensure_emitter_dirs("../escape")
    with pytest.raises(ValueError):
        spool.ensure_emitter_dirs(".removals")


# ---------------------------------------------------------------------------
# emit() — the consumer-side reference
# ---------------------------------------------------------------------------


def test_emit_publishes_canonical_v1_envelope_0600(spool):
    p = es.emit(_edir(spool), EV)
    assert p.parent == _emissions_dir(spool)
    assert p.name.startswith(f"{EV}--") and p.name.endswith(".json")
    assert oct(p.stat().st_mode)[-3:] == "600"
    assert p.read_bytes() == b'{"v":1}'


def test_emit_two_calls_never_collide(spool):
    p1 = es.emit(_edir(spool), EV)
    p2 = es.emit(_edir(spool), EV)
    assert p1 != p2
    assert p1.exists() and p2.exists()


def test_emit_refuses_unsafe_event_name(spool):
    with pytest.raises(ValueError):
        es.emit(_edir(spool), "bad--name")
    with pytest.raises(ValueError):
        es.emit(_edir(spool), "../escape")


def test_emit_leaves_no_part_residue_on_success(spool):
    es.emit(_edir(spool), EV)
    names = os.listdir(_emissions_dir(spool))
    assert all(not n.startswith(".part-") for n in names)


def test_emit_is_exactly_once_under_two_threads(spool):
    """Two real threads emitting concurrently must both land intact —
    the write-then-rename sequence is race-safe under contention, even
    though (unlike callback's claim) there is no winner to arbitrate:
    each call's random suffix is its own."""
    start = threading.Barrier(2)
    results = [None, None]

    def worker(i):
        start.wait()
        results[i] = es.emit(_edir(spool), EV)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results[0] is not None and results[1] is not None
    assert results[0] != results[1]
    assert results[0].read_bytes() == b'{"v":1}'
    assert results[1].read_bytes() == b'{"v":1}'


# ---------------------------------------------------------------------------
# fold_pass — happy path / open / idle / cycling
# ---------------------------------------------------------------------------


def test_fold_pass_noop_with_no_emissions_no_state(spool):
    changed = spool.fold_pass(R(S1), 1000.0)
    assert changed == []
    assert _read_state(spool) is None


def test_fold_pass_does_not_open_with_empty_routed_cohort(spool):
    _emit(spool, when=1000.0)
    changed = spool.fold_pass({}, 2000.0)
    assert changed == []
    assert _read_state(spool) is None


def test_fold_pass_opens_gen1_and_mints_pending_records(spool):
    _emit(spool, when=1000.0)
    changed = spool.fold_pass(R(S1, S2), 2000.0)
    assert set(changed) == {(E, EV, S1), (E, EV, S2)}
    state = _read_state(spool)
    assert state["gen"] == 1
    assert state["cohort"] == sorted([S1, S2])
    assert len(state["folded"]) == 1        # the one emission's token —
    # the FIELD records what was folded into this generation; the FILE
    # itself is what gets unlinked
    assert list(_emissions_dir(spool).glob(f"{EV}--*")) == []

    rec1 = _read_delivery(spool, S1)
    assert rec1["status"] == "pending" and rec1["gen"] == 1
    rec2 = _read_delivery(spool, S2)
    assert rec2["status"] == "pending" and rec2["gen"] == 1
    assert rec1["ack_token"] != rec2["ack_token"]


def test_fold_pass_not_idle_while_a_delivery_is_pending(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 2000.0)
    _emit(spool, when=2100.0)               # arrives mid-generation
    changed = spool.fold_pass(R(S1), 2200.0)
    assert changed == []                    # not idle: gen1 still pending
    state = _read_state(spool)
    assert state["gen"] == 1
    # the new emission is neither folded nor deleted — it survives
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))


def test_emit_during_in_flight_generation_is_never_lost(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 2000.0)
    new_emission = _emit(spool, when=2100.0)
    spool.fold_pass(R(S1), 2200.0)          # not idle — folds nothing
    assert new_emission.exists()

    # ack the gen-1 record -> idle
    rec = _read_delivery(spool, S1)
    outcome, sub = spool.ack(E, EV, rec["ack_token"], now=2300.0)
    assert (outcome, sub) == ("acked", S1)

    changed = spool.fold_pass(R(S1), 2400.0)
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert state["gen"] == 2
    assert not new_emission.exists()        # now folded away


def test_second_cycle_after_terminal_opens_gen3(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    spool.ack(E, EV, rec["ack_token"], now=1200.0)

    _emit(spool, when=1300.0)
    spool.fold_pass(R(S1), 1400.0)
    rec = _read_delivery(spool, S1)
    assert rec["gen"] == 2
    spool.ack(E, EV, rec["ack_token"], now=1500.0)

    _emit(spool, when=1600.0)
    spool.fold_pass(R(S1), 1700.0)
    rec = _read_delivery(spool, S1)
    assert rec["gen"] == 3


def test_late_routed_subscriber_joins_the_next_generation_only(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)          # gen1, cohort={S1}
    assert _read_delivery(spool, S2) is None

    rec = _read_delivery(spool, S1)
    spool.ack(E, EV, rec["ack_token"], now=1200.0)   # idle
    _emit(spool, when=1300.0)
    changed = spool.fold_pass(R(S1, S2), 1400.0)      # S2 now routed
    assert set(changed) == {(E, EV, S1), (E, EV, S2)}
    state = _read_state(spool)
    assert state["gen"] == 2
    assert state["cohort"] == sorted([S1, S2])


def test_fold_batch_bound_65_folds_64_oldest_65th_survives(spool):
    paths = _emit_n(spool, 65, start=1000.0, step=1.0)
    changed = spool.fold_pass(R(S1), 2000.0)
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert state["gen"] == 1
    assert len(state["folded"]) == FOLD_BATCH_MAX
    remaining = list(_emissions_dir(spool).glob(f"{EV}--*.json"))
    assert len(remaining) == 1
    assert remaining[0].name == paths[-1].name    # the newest one survives


def test_folded_remainder_folds_into_the_next_generation(spool):
    _emit_n(spool, 65, start=1000.0, step=1.0)
    spool.fold_pass(R(S1), 2000.0)
    rec = _read_delivery(spool, S1)
    spool.ack(E, EV, rec["ack_token"], now=2100.0)
    changed = spool.fold_pass(R(S1), 2200.0)
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert state["gen"] == 2
    assert len(state["folded"]) == 1
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []


# ---------------------------------------------------------------------------
# fold_pass — non-resurrection (Sol-r3 #2 pin)
# ---------------------------------------------------------------------------


def test_removed_mid_generation_member_is_never_recreated(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1, S2), 1100.0)      # gen1, cohort={S1,S2}
    assert _read_delivery(spool, S2) is not None

    # S2 is de-routed mid-generation; a REPAIR pass must not touch it,
    # nor recreate it once it is torn down.
    spool.fold_pass(R(S1), 1200.0)          # S2 no longer routed
    rec2 = _read_delivery(spool, S2)
    assert rec2 is not None and rec2["status"] == "pending"   # untouched

    rec1 = _read_delivery(spool, S1)
    spool.ack(E, EV, rec1["ack_token"], now=1300.0)
    # sweep terminalizes S2's now-unrouted record (a later task's concern
    # for dispatch, but sweep is what actually flips it here)
    spool.sweep(R(S1), installed={S1, S2}, registry_valid=True, now=1400.0)
    rec2 = _read_delivery(spool, S2)
    assert rec2["status"] == "done" and rec2["outcome"] == "revoked"

    _emit(spool, when=1500.0)
    changed = spool.fold_pass(R(S1), 1600.0)   # idle now (S1 done via ack,
    # S2 done via sweep) — opens gen2 WITHOUT S2
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert state["gen"] == 2
    assert state["cohort"] == [S1]


# ---------------------------------------------------------------------------
# fold_pass — reconstruction-first healing (Sol-r3 #3 / Terra-r3 #3 pins)
# ---------------------------------------------------------------------------


def test_reconstruction_rebuilds_state_at_max_record_gen_no_emission(spool):
    now = 5000.0
    rec = ea.new_record(E, EV, S1, 4, "tok-preserved", now - 100)
    spool.ensure_emitter_dirs(E)
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(rec).decode())
    assert _read_state(spool) is None

    changed = spool.fold_pass(R(S1), now)
    assert changed == []                     # nothing NEW minted — the
    # surviving record's ladder is preserved verbatim
    state = _read_state(spool)
    assert state is not None
    assert state["gen"] == 4
    assert state["cohort"] == [S1]
    assert state["folded"] == []
    rec_after = _read_delivery(spool, S1)
    assert rec_after["ack_token"] == "tok-preserved"
    assert rec_after["gen"] == 4
    assert rec_after["status"] == "pending"


def test_reconstruction_with_queued_emission_also_repairs_and_stays_pending(spool):
    now = 5000.0
    rec = ea.new_record(E, EV, S1, 4, "tok-preserved", now - 100)
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(rec).decode())
    _emit(spool, when=now - 50)              # queued while state was lost

    changed = spool.fold_pass(R(S1), now)
    state = _read_state(spool)
    assert state["gen"] == 4                 # never re-minted below max
    # not idle (S1 still pending) — the queued emission must survive,
    # unfolded, until S1's gen-4 delivery resolves
    assert changed == []
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))


def test_reconstruction_quarantines_the_corrupt_original_and_surfaces_issue(spool):
    now = 5000.0
    rec = ea.new_record(E, EV, S1, 2, "tok-x", now - 10)
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(rec).decode())
    _write_raw(_state_dir(spool) / f"{EV}.json", "{not json")

    spool.fold_pass(R(S1), now)
    state = _read_state(spool)
    assert state["gen"] == 2

    corrupt = list(_state_dir(spool).glob(".corrupt-*"))
    assert len(corrupt) == 1
    issues = spool.spool_issues()
    assert any(i["kind"] == "corrupt_state" and i["emitter"] == E
              for i in issues)


def test_state_lost_with_no_records_silently_resets_to_gen1_no_issue(spool):
    now = 5000.0
    _emit(spool, when=now - 10)
    changed = spool.fold_pass(R(S1), now)
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert state["gen"] == 1
    assert spool.spool_issues() == []


def test_reconstruction_repair_excludes_a_subscriber_consented_mid_crash(spool):
    """The subscriber consented AFTER the crash lost the state file must
    NOT join the reconstructed generation — cohort comes from the
    surviving records, never the live routed map."""
    now = 5000.0
    rec = ea.new_record(E, EV, S1, 2, "tok-x", now - 10)
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(rec).decode())

    spool.fold_pass(R(S1, S2), now)          # S2 routed NOW, but was not
    # a gen-2 record holder
    state = _read_state(spool)
    assert state["gen"] == 2
    assert state["cohort"] == [S1]
    assert _read_delivery(spool, S2) is None


# ---------------------------------------------------------------------------
# fold_pass — crash injection between OPEN's steps
# ---------------------------------------------------------------------------


def _boom_after(monkeypatch, target_module, name, n):
    """Raise on the *n*-th call to ``target_module.<name>``, delegating to
    the original on every other call."""
    orig = getattr(target_module, name)
    calls = {"i": 0}

    def wrapper(*a, **kw):
        calls["i"] += 1
        if calls["i"] == n:
            raise RuntimeError("simulated crash")
        return orig(*a, **kw)

    monkeypatch.setattr(target_module, name, wrapper)
    return calls


def test_crash_between_state_write_and_record_upserts_self_heals(spool, monkeypatch):
    _emit_n(spool, 3, start=1000.0)
    # The state write succeeds; the crash lands on the FIRST
    # _write_delivery call, i.e. the record-upsert step of OPEN.
    _boom_after(monkeypatch, es.EventSpool, "_write_delivery", 1)
    changed = spool.fold_pass(R(S1, S2), 2000.0)
    assert changed == []                     # the exception aborted this
    # pair's fold before anything was recorded as "changed"
    state = _read_state(spool)
    assert state is not None and state["gen"] == 1   # state IS durable

    monkeypatch.undo()
    changed = spool.fold_pass(R(S1, S2), 2100.0)
    assert set(changed) == {(E, EV, S1), (E, EV, S2)}
    assert _read_delivery(spool, S1)["gen"] == 1
    assert _read_delivery(spool, S2)["gen"] == 1
    # folded emissions are gone once the pass completes cleanly
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []


def test_crash_between_record_upserts_and_unlink_self_heals(spool, monkeypatch):
    _emit_n(spool, 3, start=1000.0)
    # State + records are written durably; the crash lands on the FIRST
    # emission unlink of OPEN's final step.
    real_unlink = es._unlink_quiet
    calls = {"i": 0}

    def boom_unlink(name, dir_fd):
        calls["i"] += 1
        if calls["i"] == 1:
            raise RuntimeError("simulated crash mid-unlink")
        return real_unlink(name, dir_fd)

    monkeypatch.setattr(es, "_unlink_quiet", boom_unlink)
    changed = spool.fold_pass(R(S1), 2000.0)
    # the crash propagates out of _fold_one before it returns, so this
    # pass reports nothing changed — but the record write that already
    # happened is durable on disk regardless (proven below)
    assert changed == []
    state = _read_state(spool)
    assert state["gen"] == 1
    rec = _read_delivery(spool, S1)
    assert rec is not None and rec["gen"] == 1 and rec["status"] == "pending"
    # at least one emission is still on disk — the crash interrupted the
    # unlink loop
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json"))

    monkeypatch.undo()
    # next pass: not idle (S1 pending) so OPEN won't refire, but REPAIR's
    # unconditional folded-unlink step cleans up the survivors
    spool.fold_pass(R(S1), 2100.0)
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []


def test_crash_leaving_state_written_records_partial_then_repair_completes(spool, monkeypatch):
    _emit_n(spool, 3, start=1000.0)
    real_write = es.EventSpool._write_delivery
    calls = {"i": 0}

    def boom_write(self, dfd, event, subscriber, rec):
        calls["i"] += 1
        if subscriber == S2:
            return False                     # simulated undurable write
        return real_write(self, dfd, event, subscriber, rec)

    monkeypatch.setattr(es.EventSpool, "_write_delivery", boom_write)
    changed = spool.fold_pass(R(S1, S2), 2000.0)
    assert (E, EV, S1) in changed
    assert (E, EV, S2) not in changed
    assert _read_delivery(spool, S2) is None     # S2's write never landed

    monkeypatch.undo()
    changed = spool.fold_pass(R(S1, S2), 2100.0)   # REPAIR backfills S2
    assert changed == [(E, EV, S2)]
    assert _read_delivery(spool, S2)["gen"] == 1


# ---------------------------------------------------------------------------
# ROUTING_UNAVAILABLE — strict no-op for fold
# ---------------------------------------------------------------------------


def test_fold_pass_under_routing_unavailable_is_a_strict_noop(spool):
    _emit(spool, when=1000.0)
    _write_raw(_state_dir(spool) / f"{EV}.json", "{garbage")
    before_emissions = sorted(os.listdir(_emissions_dir(spool)))
    before_state = sorted(os.listdir(_state_dir(spool)))

    changed = spool.fold_pass(ROUTING_UNAVAILABLE, 2000.0)
    assert changed == []
    assert sorted(os.listdir(_emissions_dir(spool))) == before_emissions
    assert sorted(os.listdir(_state_dir(spool))) == before_state
    assert spool.spool_issues() == []        # no reconstruction attempted


# ---------------------------------------------------------------------------
# update_delivery_nudge
# ---------------------------------------------------------------------------


def _mutate_nudge(**kv):
    def _m(rec):
        rec.update(kv)
        return rec
    return _m


def test_update_delivery_nudge_happy_path(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    ok = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"], _mutate_nudge(nudges=1, last_nudge_ts=1150.0,
                                             next_nudge_ts=1450.0))
    assert ok is True
    after = _read_delivery(spool, S1)
    assert after["nudges"] == 1
    assert after["last_nudge_ts"] == 1150.0
    assert after["next_nudge_ts"] == 1450.0
    assert after["ack_token"] == rec["ack_token"]    # untouched


def test_update_delivery_nudge_refuses_gen_mismatch(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    ok = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"] + 1, _mutate_nudge(nudges=1))
    assert ok is False
    assert _read_delivery(spool, S1)["nudges"] == 0


def test_update_delivery_nudge_refuses_touching_disallowed_field(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    ok = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"], _mutate_nudge(ack_token="hijacked"))
    assert ok is False
    assert _read_delivery(spool, S1)["ack_token"] == rec["ack_token"]

    ok2 = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"], _mutate_nudge(gen=rec["gen"] + 1))
    assert ok2 is False


def test_update_delivery_nudge_refused_after_concurrent_ack(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    outcome, sub = spool.ack(E, EV, rec["ack_token"], now=1200.0)
    assert outcome == "acked"

    ok = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"], _mutate_nudge(nudges=1))
    assert ok is False
    after = _read_delivery(spool, S1)
    assert after["status"] == "done" and after["outcome"] == "acked"


def test_exhaustion_is_one_atomic_update(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    ok = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"],
        _mutate_nudge(status="done", outcome="exhausted", noted=True,
                      ended_ts=1500.0, next_nudge_ts=None))
    assert ok is True
    after = _read_delivery(spool, S1)
    assert after["status"] == "done"
    assert after["outcome"] == "exhausted"
    assert after["noted"] is True
    assert after["ended_ts"] == 1500.0

    # done is immutable forever — a further update always refuses
    ok2 = spool.update_delivery_nudge(
        E, EV, S1, rec["gen"], _mutate_nudge(noted=False))
    assert ok2 is False


def test_update_delivery_nudge_unknown_record_returns_false(spool):
    assert spool.update_delivery_nudge(E, EV, "nobody", 1, _mutate_nudge(nudges=1)) is False


# ---------------------------------------------------------------------------
# ack — typed results + stale-token CAS
# ---------------------------------------------------------------------------


def test_ack_acked_then_already_done_no_mutation(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    rec = _read_delivery(spool, S1)
    token = rec["ack_token"]

    outcome, sub = spool.ack(E, EV, token, now=1200.0)
    assert (outcome, sub) == ("acked", S1)
    done_rec = _read_delivery(spool, S1)
    assert done_rec["status"] == "done" and done_rec["outcome"] == "acked"

    outcome2, sub2 = spool.ack(E, EV, token, now=1300.0)
    assert (outcome2, sub2) == ("already_done", S1)
    unchanged = _read_delivery(spool, S1)
    assert unchanged == done_rec             # byte-for-byte no mutation


def test_ack_no_match_for_unknown_token(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    outcome, sub = spool.ack(E, EV, "not-a-real-token", now=1200.0)
    assert (outcome, sub) == ("no_match", None)


def test_ack_no_match_for_unknown_emitter_or_event(spool):
    assert spool.ack("ghost", EV, "x", now=1000.0) == ("no_match", None)
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    assert spool.ack(E, "ghost-event", "x", now=1200.0) == ("no_match", None)


def test_stale_token_cas_after_fold_rotates_it(spool):
    now = 5000.0
    old_rec = ea.new_record(E, EV, S1, 1, "tok-old", now - 100)
    _write_raw(_delivery_path(spool, S1),
              es.canonical_marker_bytes(old_rec).decode())
    state = {"v": 1, "event": EV, "gen": 2, "cohort": [S1], "folded": [],
            "opened_ts": now - 50}
    _write_raw(_state_dir(spool) / f"{EV}.json",
              es.canonical_marker_bytes(state).decode())

    spool.fold_pass(R(S1), now)              # REPAIR upserts a fresh token
    new_rec = _read_delivery(spool, S1)
    assert new_rec["gen"] == 2
    assert new_rec["ack_token"] != "tok-old"

    assert spool.ack(E, EV, "tok-old", now=now + 10) == ("no_match", None)
    outcome, sub = spool.ack(E, EV, new_rec["ack_token"], now=now + 10)
    assert (outcome, sub) == ("acked", S1)


# ---------------------------------------------------------------------------
# sweep — the watermark trio (Sol/Terra-r3 #1 pins)
# ---------------------------------------------------------------------------


def test_sweep_deletes_emissions_of_an_unrouted_pair_next_pass(spool):
    p = _emit(spool, when=1000.0)
    spool.sweep({}, installed=set(), registry_valid=True, now=1100.0)
    assert not p.exists()


def test_sweep_never_resurrects_an_already_deleted_emission_on_late_consent(spool):
    p = _emit(spool, when=1000.0)
    spool.sweep({}, installed=set(), registry_valid=True, now=1100.0)
    assert not p.exists()
    spool.sweep(R(S1), installed={S1}, registry_valid=True, now=1200.0)
    assert not p.exists()                    # gone forever


def test_sweep_leaves_a_routed_pairs_queue_untouched_when_a_second_subscriber_joins(spool):
    p1 = _emit(spool, when=1000.0)
    spool.sweep(R(S1), installed={S1}, registry_valid=True, now=1100.0)
    assert p1.exists()
    p2 = _emit(spool, when=1150.0)
    spool.sweep(R(S1, S2), installed={S1, S2}, registry_valid=True, now=1200.0)
    assert p1.exists() and p2.exists()


def test_sweep_post_consent_emission_survives(spool):
    p = _emit(spool, when=1000.0)
    # already routed from the start
    spool.sweep(R(S1), installed={S1}, registry_valid=True, now=1100.0)
    assert p.exists()


# ---------------------------------------------------------------------------
# sweep — disk-pressure valve
# ---------------------------------------------------------------------------


def test_sweep_disk_pressure_valve_deletes_oldest_overflow_with_log(spool, caplog):
    paths = _emit_n(spool, MAX_EMISSION_FILES + 1, start=1000.0, step=1.0)
    with caplog.at_level(logging.WARNING, logger="event_spool"):
        report = spool.sweep(R(S1), installed={S1}, registry_valid=True,
                             now=3000.0)
    assert report.deleted_valve == 1
    remaining = sorted(os.listdir(_emissions_dir(spool)))
    assert len(remaining) == MAX_EMISSION_FILES
    assert not paths[0].exists()             # the single oldest is gone
    assert all(p.exists() for p in paths[1:])
    assert any("disk-pressure valve" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# sweep — corrupt delivery quarantine
# ---------------------------------------------------------------------------


def test_sweep_quarantines_corrupt_delivery_file_and_surfaces_issue(spool):
    _write_raw(_delivery_path(spool, S1), "{not json")
    report = spool.sweep(R(S1), installed={S1}, registry_valid=True, now=1000.0)
    assert report.quarantined_delivery == 1
    assert not _delivery_path(spool, S1).exists()
    quarantined = list(_delivery_dir(spool).glob(".corrupt-*"))
    assert len(quarantined) == 1
    issues = spool.spool_issues()
    assert any(i["kind"] == "corrupt_delivery" for i in issues)


def test_corrupt_delivery_does_not_block_idle_and_is_unackable(spool):
    _write_raw(_delivery_path(spool, S1), "{not json")
    _emit(spool, when=1000.0)
    # idle: the invalid record is excluded from the pending scan
    changed = spool.fold_pass(R(S1), 2000.0)
    assert changed == [(E, EV, S1)]
    state = _read_state(spool)
    assert state["gen"] == 1


def test_no_match_ack_for_a_token_that_was_never_valid(spool):
    _write_raw(_delivery_path(spool, S1), "{not json")
    assert spool.ack(E, EV, "anything", now=1000.0) == ("no_match", None)


# ---------------------------------------------------------------------------
# sweep — removed-vs-revoked classification
# ---------------------------------------------------------------------------


def test_sweep_classifies_revoked_when_subscriber_still_installed(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    spool.sweep({}, installed={S1}, registry_valid=True, now=1200.0)
    rec = _read_delivery(spool, S1)
    assert rec["status"] == "done" and rec["outcome"] == "revoked"


def test_sweep_classifies_removed_when_subscriber_no_longer_installed(spool, monkeypatch):
    # A subscriber unrouted AND already uninstalled goes terminal and is
    # dropped in the SAME pass (nothing gates the drop on a second pass —
    # only on the removal record being durable first). Spy on the write
    # to observe the "removed" outcome before the file is unlinked.
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    written = []
    orig = es.EventSpool._write_delivery

    def spy(self, dfd, event, subscriber, rec):
        written.append(dict(rec))
        return orig(self, dfd, event, subscriber, rec)

    monkeypatch.setattr(es.EventSpool, "_write_delivery", spy)
    report = spool.sweep({}, installed=set(), registry_valid=True, now=1200.0)
    assert report.terminalized == 1
    assert report.dropped_records == 1
    assert _read_delivery(spool, S1) is None
    assert any(r["outcome"] == "removed" for r in written)


def test_sweep_retains_unrouted_records_as_tombstones_until_plugin_removed(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    spool.sweep({}, installed={S1}, registry_valid=True, now=1200.0)
    assert _delivery_path(spool, S1).exists()      # tombstoned, not dropped
    assert spool.list_removal_records() == []


# ---------------------------------------------------------------------------
# sweep — removal records: tagged union, drop-only-after-durable
# ---------------------------------------------------------------------------


def test_sweep_drops_tombstoned_record_only_after_durable_removal_record(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    # first pass: S1 unrouted but still installed -> revoked tombstone
    spool.sweep({}, installed={S1}, registry_valid=True, now=1200.0)
    assert _delivery_path(spool, S1).exists()

    # second pass: S1's plugin is now uninstalled -> drop, with a removal
    # record written first
    report = spool.sweep({}, installed=set(), registry_valid=True, now=1300.0)
    assert report.removal_records_written == 1
    assert report.dropped_records == 1
    assert not _delivery_path(spool, S1).exists()

    records = spool.list_removal_records()
    assert len(records) == 1
    name, rec = records[0]
    assert rec["plugin"] == S1
    assert rec["noted"] is False
    assert rec["entries"] == [{"kind": "record", "emitter": E, "event": EV,
                               "gen": 1}]


def test_removal_record_tagged_union_includes_corrupt_entry(spool):
    _write_raw(_delivery_path(spool, "ghost"), "{not json")
    # attribute the corrupt file to "ghost", never installed
    report = spool.sweep({}, installed=set(), registry_valid=True, now=1000.0)
    assert report.quarantined_delivery == 1
    assert report.removal_records_written == 1
    assert report.dropped_corrupt == 1

    records = spool.list_removal_records()
    assert len(records) == 1
    name, rec = records[0]
    assert rec["plugin"] == "ghost"
    assert len(rec["entries"]) == 1
    entry = rec["entries"][0]
    assert entry["kind"] == "corrupt"
    assert entry["file"].startswith(".corrupt-")

    # and the quarantined file itself is gone (deleted after durability)
    assert list(_delivery_dir(spool).glob(".corrupt-*")) == []


def test_removal_record_batches_record_and_corrupt_entries_for_one_subscriber(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    _write_raw(_delivery_dir(spool) / f"other-event--{S1}.json", "{not json")

    report = spool.sweep({}, installed=set(), registry_valid=True, now=1200.0)
    assert report.removal_records_written == 1
    records = spool.list_removal_records()
    assert len(records) == 1
    _, rec = records[0]
    kinds = sorted(e["kind"] for e in rec["entries"])
    assert kinds == ["corrupt", "record"]


# ---------------------------------------------------------------------------
# sweep — ROUTING_UNAVAILABLE strict no-op (Sol-r5 #1 pin)
# ---------------------------------------------------------------------------


def test_sweep_under_routing_unavailable_only_does_part_ttl(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    part = _emissions_dir(spool) / ".part-deadbeef"
    part.write_bytes(b"x")
    _utime(part, 1000.0 - TEMP_TTL_S - 100)

    report = spool.sweep(ROUTING_UNAVAILABLE, installed=set(),
                         registry_valid=True, now=2000.0)
    assert not part.exists()                 # part-TTL housekeeping ran
    assert report.deleted_watermark == 0
    assert report.terminalized == 0
    assert report.removal_records_written == 0
    # the pending delivery is untouched — no destructive action ran
    rec = _read_delivery(spool, S1)
    assert rec["status"] == "pending"
    assert list(_emissions_dir(spool).glob(f"{EV}--*.json")) == []  # folded
    # already, from the earlier fold_pass call — unrelated to this sweep


# ---------------------------------------------------------------------------
# part-file TTL sweep
# ---------------------------------------------------------------------------


def test_sweep_deletes_stale_part_files(spool):
    part = _emissions_dir(spool) / ".part-cafebabe"
    part.write_bytes(b"x")
    _utime(part, 0.0)
    spool.sweep({}, installed=set(), registry_valid=True, now=TEMP_TTL_S + 100)
    assert not part.exists()


def test_sweep_keeps_a_fresh_part_file(spool):
    part = _emissions_dir(spool) / ".part-cafebabe"
    part.write_bytes(b"x")
    _utime(part, 1000.0)
    spool.sweep({}, installed=set(), registry_valid=True, now=1000.0 + 10)
    assert part.exists()


# ---------------------------------------------------------------------------
# removal records — list / mark noted / prune
# ---------------------------------------------------------------------------


def test_list_removal_records_retires_unreadable_entries(spool):
    (Path(spool.root) / ".removals").mkdir(exist_ok=True)
    bad = Path(spool.root) / ".removals" / "ghost-deadbeefdeadbeefdeadbeefdeadbeef.json"
    bad.write_text("{not json")
    assert spool.list_removal_records() == []
    assert not bad.exists()


def test_mark_removal_noted_then_prune_after_window(spool):
    _write_raw(_delivery_path(spool, "ghost"), "{not json")
    spool.sweep({}, installed=set(), registry_valid=True, now=1000.0)
    records = spool.list_removal_records()
    assert len(records) == 1
    name, rec = records[0]

    assert spool.mark_removal_noted(name, now=1100.0) is True
    records = spool.list_removal_records()
    _, rec = records[0]
    assert rec["noted"] is True and rec["noted_ts"] == 1100.0

    pruned = spool.prune_removal_records(now=1100.0 + es.REMOVAL_RECORD_PRUNE_S - 10)
    assert pruned == 0
    pruned = spool.prune_removal_records(now=1100.0 + es.REMOVAL_RECORD_PRUNE_S + 10)
    assert pruned == 1
    assert spool.list_removal_records() == []


def test_prune_removes_un_noted_records_past_max_age(spool):
    _write_raw(_delivery_path(spool, "ghost"), "{not json")
    spool.sweep({}, installed=set(), registry_valid=True, now=1000.0)
    pruned = spool.prune_removal_records(
        now=1000.0 + es.REMOVAL_RECORD_MAX_AGE_S + 10)
    assert pruned == 1


# ---------------------------------------------------------------------------
# orphan GC — gated
# ---------------------------------------------------------------------------


def _backdate_tree(root: Path, when: float) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            _utime(Path(dirpath) / name, when)
        _utime(Path(dirpath), when)


def test_gc_orphan_dirs_is_a_noop_when_registry_invalid(spool):
    _backdate_tree(_edir(spool), 0.0)
    removed = spool.gc_orphan_dirs(registry_valid=False, member_plugins=set(),
                                   now=QUIESCENCE_S + 10000)
    assert removed == []
    assert _edir(spool).exists()


def test_gc_orphan_dirs_removes_a_quiescent_uninstalled_emitter(spool):
    _backdate_tree(_edir(spool), 0.0)
    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                   now=QUIESCENCE_S + 10000)
    assert removed == [E]
    assert not _edir(spool).exists()


def test_gc_orphan_dirs_skips_a_member_plugin(spool):
    _backdate_tree(_edir(spool), 0.0)
    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins={E},
                                   now=QUIESCENCE_S + 10000)
    assert removed == []
    assert _edir(spool).exists()


def test_gc_orphan_dirs_skips_a_non_quiescent_dir(spool):
    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                   now=time.time())
    assert removed == []
    assert _edir(spool).exists()


def test_gc_orphan_dirs_writes_removal_record_when_inventory_nonempty(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    _backdate_tree(_edir(spool), 0.0)
    removed = spool.gc_orphan_dirs(registry_valid=True, member_plugins=set(),
                                   now=QUIESCENCE_S + 10000)
    assert removed == [E]
    records = spool.list_removal_records()
    assert len(records) == 1
    _, rec = records[0]
    assert rec["plugin"] == E
    assert rec["entries"] == [{"kind": "record", "emitter": E, "event": EV,
                               "gen": 1}]


# ---------------------------------------------------------------------------
# recovery_pass composition
# ---------------------------------------------------------------------------


def test_recovery_pass_folds_and_sweeps(spool):
    _emit(spool, when=1000.0)
    report = spool.recovery_pass(R(S1), installed={S1}, registry_valid=True,
                                 now=2000.0, boot=False)
    assert report.opened == [(E, EV, S1)]
    assert report.sweep is not None
    state = _read_state(spool)
    assert state["gen"] == 1


def test_recovery_pass_runs_gc_only_on_boot_with_valid_registry(spool):
    _backdate_tree(_edir(spool), 0.0)
    report = spool.recovery_pass({}, installed=set(), registry_valid=True,
                                 now=QUIESCENCE_S + 10000, boot=False)
    assert report.gc_removed == []
    assert _edir(spool).exists()

    report2 = spool.recovery_pass({}, installed=set(), registry_valid=True,
                                  now=QUIESCENCE_S + 20000, boot=True)
    assert report2.gc_removed == [E]


# ---------------------------------------------------------------------------
# spool_issues
# ---------------------------------------------------------------------------


def test_spool_issues_empty_on_a_clean_spool(spool):
    _emit(spool, when=1000.0)
    spool.fold_pass(R(S1), 1100.0)
    assert spool.spool_issues() == []


# ---------------------------------------------------------------------------
# init_spool / get_spool / env override
# ---------------------------------------------------------------------------


def test_init_and_get_spool_singleton(tmp_path, monkeypatch):
    monkeypatch.setenv(es.SPOOL_ROOT_ENV, str(tmp_path / "envroot"))
    s = es.init_spool()
    try:
        assert es.get_spool() is s
        assert s.root == tmp_path / "envroot"
    finally:
        s.close()


def test_get_spool_before_init_is_none_or_prior():
    # get_spool reflects whatever the LAST init_spool call in this process
    # set — no assumption about ordering across tests beyond "callable
    # without raising".
    es.get_spool()


def test_module_level_spool_issues_degrades_quietly_without_a_spool(monkeypatch):
    monkeypatch.setattr(es, "_SPOOL", None)
    assert es.spool_issues() == []
