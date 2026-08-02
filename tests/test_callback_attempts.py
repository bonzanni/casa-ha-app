"""Tests for callback_attempts — the pure attempt schema/envelope/schedule leaf.

Pure-function tests only: every call passes ``now=`` explicitly, no clocks,
no I/O, no fixtures. Schedule expectations use the spec's literal offsets
(0/60/180/480 result-phase from publish time; 1800/7200 outcome-phase from
``ended_ts``; deferral ``now + min(1800, 60 * 2**deferrals)``).
"""

import json

import callback_attempts as ca

H = "ab" * 32  # a well-formed 64-hex state hash
T = 10_000.0


def _envelope_of_size(total: int) -> bytes:
    """A VALID v2 envelope of exactly ``total`` bytes (size is the only variable)."""
    prefix, suffix = b'{"v":2,"meta":"', b'"}'
    pad = total - len(prefix) - len(suffix)
    assert pad >= 0
    return prefix + b"a" * pad + suffix


# ---------------------------------------------------------------------------
# parse_envelope
# ---------------------------------------------------------------------------

def test_envelope_v2_echoes_meta_value():
    meta = {"kind": "renewal", "bank": "x", "n": 3}
    out = ca.parse_envelope(json.dumps({"v": 2, "meta": meta}).encode("utf-8"))
    assert out == {"v": 2, "meta": meta}


def test_envelope_v1_meta_is_none():
    assert ca.parse_envelope(b'{"v": 1}') == {"v": 1, "meta": None}
    # A v1 envelope never carries meta — even a scribbled key is ignored.
    assert ca.parse_envelope(b'{"v": 1, "meta": "x"}') == {"v": 1, "meta": None}
    # v2 with absent meta degrades the same way.
    assert ca.parse_envelope(b'{"v": 2}') == {"v": 2, "meta": None}


def test_envelope_oversized_is_none():
    assert ca.parse_envelope(_envelope_of_size(ca.ENVELOPE_MAX_BYTES + 1)) is None
    # Boundary: exactly the cap still parses.
    assert ca.parse_envelope(_envelope_of_size(ca.ENVELOPE_MAX_BYTES)) is not None


def test_envelope_defects_are_none():
    assert ca.parse_envelope(b"[1, 2]") is None          # non-object JSON
    assert ca.parse_envelope(b'"hello"') is None         # non-object JSON
    assert ca.parse_envelope(b"\xff\xfe{}") is None      # not UTF-8
    assert ca.parse_envelope(b"{") is None               # not JSON at all
    assert ca.parse_envelope(b'{"v": 3}') is None        # unknown version
    assert ca.parse_envelope(b'{"meta": 1}') is None     # version absent


def test_envelope_unknown_keys_dropped():
    out = ca.parse_envelope(b'{"v": 2, "meta": 1, "extra": "boo", "state": "s"}')
    assert out == {"v": 2, "meta": 1}
    assert set(out) == {"v", "meta"}


# ---------------------------------------------------------------------------
# new_attempt
# ---------------------------------------------------------------------------

def test_new_attempt_result_ready_next_nudge_is_now():
    rec = ca.new_attempt(state_hash=H, minted_ts=T - 30.0,
                         status="result_ready", meta={"k": 1}, now=T)
    assert rec["next_nudge_ts"] == T  # RESULT_PHASE_OFFSETS[0] == 0 from publish
    assert rec["status"] == "result_ready"
    assert rec["outcome"] is None
    assert rec["claimed"] is False
    assert rec["nudges"] == 0 and rec["deferrals"] == 0
    assert rec["last_nudge_ts"] is None and rec["ended_ts"] is None
    assert rec["noted"] is False
    assert rec["meta"] == {"k": 1}


def test_new_attempt_awaiting_redirect_has_no_nudge():
    # Nudges only exist once a result (or terminal outcome) exists.
    rec = ca.new_attempt(state_hash=H, minted_ts=None,
                         status="awaiting_redirect", now=T)
    assert rec["next_nudge_ts"] is None
    assert rec["minted_ts"] is None  # legacy/collect-held materialization


# ---------------------------------------------------------------------------
# validate_attempt
# ---------------------------------------------------------------------------

def _good() -> dict:
    return ca.new_attempt(state_hash=H, minted_ts=T - 30.0,
                          status="result_ready", meta={"k": 1}, now=T)


def test_validate_accepts_json_round_trip():
    rec = _good()
    assert ca.validate_attempt(json.loads(json.dumps(rec))) == rec
    # A terminal record round-trips too.
    done = ca.terminalize(rec, "expired_unread", now=T + 900.0)
    assert ca.validate_attempt(json.loads(json.dumps(done))) == done


def test_validate_rejects_key_set_defects():
    assert ca.validate_attempt(dict(_good(), bogus=1)) is None      # extra key
    missing = _good()
    del missing["nudges"]
    assert ca.validate_attempt(missing) is None                     # missing key


def test_validate_rejects_type_defects():
    assert ca.validate_attempt(dict(_good(), nudges=True)) is None      # bool int
    assert ca.validate_attempt(dict(_good(), deferrals=-1)) is None     # negative
    assert ca.validate_attempt(dict(_good(), claimed=0)) is None        # int bool
    assert ca.validate_attempt(dict(_good(), noted=1)) is None          # int bool
    assert ca.validate_attempt(dict(_good(), next_nudge_ts=True)) is None
    assert ca.validate_attempt(dict(_good(), ended_ts="soon")) is None
    assert ca.validate_attempt(dict(_good(), state_hash=42)) is None


def test_validate_rejects_unknown_status_and_outcome():
    assert ca.validate_attempt(dict(_good(), status="weird")) is None
    assert ca.validate_attempt(dict(_good(), outcome="weird")) is None


def test_validate_accepts_legacy_none_minted_ts():
    assert ca.validate_attempt(dict(_good(), minted_ts=None)) is not None


def test_validate_is_total():
    # Never raises, whatever it is fed.
    for junk in (None, [], "x", 42, b"{}", {"v": 1}):
        assert ca.validate_attempt(junk) is None


# ---------------------------------------------------------------------------
# terminalize
# ---------------------------------------------------------------------------

def test_terminalize_idempotent_and_pure():
    rec = _good()
    done = ca.terminalize(rec, "expired_unread", now=T + 900.0)
    assert done["status"] == "done"
    assert done["outcome"] == "expired_unread"
    assert done["ended_ts"] == T + 900.0
    assert done["next_nudge_ts"] == T + 900.0 + 1800.0  # budget remains
    # Idempotent: same outcome again (later clock) is a no-op copy.
    assert ca.terminalize(done, "expired_unread", now=T + 950.0) == done
    # Pure: the input record was not mutated.
    assert rec["status"] == "result_ready" and rec["ended_ts"] is None


def test_terminalize_collected_never_nudges():
    done = ca.terminalize(_good(), "collected", now=T + 5.0)
    assert done["next_nudge_ts"] is None
    # And an accepted-dispatch query on a collected terminal is None too.
    assert ca.next_nudge_after_accept(done, now=T + 5.0) is None


def test_terminalize_exhausted_budget_no_nudge():
    spent = dict(_good(), nudges=ca.MAX_NUDGES)
    assert ca.terminalize(spent, "expired_unread", now=T)["next_nudge_ts"] is None


def test_terminalize_claimed_override():
    done = ca.terminalize(_good(), "expired_unread", now=T, claimed=True)
    assert done["claimed"] is True
    # None leaves the record's own flag alone.
    assert ca.terminalize(_good(), "expired_unread", now=T)["claimed"] is False


# ---------------------------------------------------------------------------
# nudge schedule
# ---------------------------------------------------------------------------

def _accept(rec: dict, now: float) -> dict:
    """Apply one bus-ACCEPTED dispatch the way the worker will."""
    nxt = ca.next_nudge_after_accept(rec, now=now)
    return dict(rec, nudges=rec["nudges"] + 1, last_nudge_ts=now,
                next_nudge_ts=nxt, deferrals=0)


def test_accept_schedule_walks_offsets_then_outcome_phase():
    rec = ca.new_attempt(state_hash=H, minted_ts=None,
                         status="result_ready", now=T)
    assert rec["next_nudge_ts"] == T                       # +0 from publish
    rec = _accept(rec, now=T)
    assert rec["next_nudge_ts"] == T + 60.0                # +60
    rec = _accept(rec, now=T + 60.0)
    assert rec["next_nudge_ts"] == T + 180.0               # +3 m
    rec = _accept(rec, now=T + 180.0)
    assert rec["next_nudge_ts"] == T + 480.0               # +8 m
    rec = _accept(rec, now=T + 480.0)
    assert rec["next_nudge_ts"] is None                    # result phase spent

    ended = T + 900.0                                      # sweep terminalizes
    rec = ca.terminalize(rec, "expired_unread", now=ended)
    assert rec["next_nudge_ts"] == ended + 1800.0          # +30 m from ended_ts
    rec = _accept(rec, now=ended + 1800.0)
    assert rec["next_nudge_ts"] == ended + 7200.0          # +2 h from ended_ts
    rec = _accept(rec, now=ended + 7200.0)
    assert rec["next_nudge_ts"] is None                    # budget of 6 spent
    assert rec["nudges"] == ca.MAX_NUDGES


def test_accept_pure_no_input_mutation():
    rec = ca.new_attempt(state_hash=H, minted_ts=None,
                         status="result_ready", now=T)
    before = json.dumps(rec, sort_keys=True)
    ca.next_nudge_after_accept(rec, now=T)
    assert json.dumps(rec, sort_keys=True) == before


def test_reject_deferral_doubles_and_caps():
    rec = ca.new_attempt(state_hash=H, minted_ts=None,
                         status="result_ready", now=T)
    n = 50_000.0
    for deferrals, expect in ((0, 60.0), (1, 120.0), (2, 240.0), (3, 480.0),
                              (4, 960.0), (5, 1800.0), (9, 1800.0)):
        got = ca.next_nudge_after_reject(dict(rec, deferrals=deferrals), now=n)
        assert got == n + expect, (deferrals, got)
