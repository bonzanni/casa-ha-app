"""Tests for event_attempts — the pure delivery-record schema/envelope/
schedule leaf for the plugin-events facility.

Pure-function tests only: every call passes ``now=`` explicitly, no clocks,
no I/O, no fixtures. Unlike callback_attempts' two-phase (result-then-
outcome) schedule, event_attempts has ONE nudge ladder — six offsets, all
anchored on the record's ``minted_ts`` — because an event-delivery record
has no separate "result published" moment: minting IS the dispatch.
"""

import json

import event_attempts as ea

E = "finance"            # emitter plugin name
EV = "invoice-created"   # event name
S = "reporting"          # subscriber plugin name
GEN = 3                  # opaque generation counter
TOK = "tok-abc123"       # opaque ack token
T = 10_000.0


def _envelope_of_size(total: int) -> bytes:
    """A VALID envelope of exactly ``total`` bytes (size is the only variable)."""
    body = b'{"v":1}'
    assert total >= len(body)
    # Pad by widening whitespace before the closing brace — still exactly
    # {"v": 1} once parsed, still exactly one key.
    pad = total - len(body)
    return b'{"v":1' + b" " * pad + b"}"


# ---------------------------------------------------------------------------
# parse_envelope
# ---------------------------------------------------------------------------

def test_envelope_v1_only_key_accepted():
    assert ea.parse_envelope(b'{"v": 1}') == {"v": 1}
    assert ea.parse_envelope(b'{"v":1}') == {"v": 1}


def test_envelope_extra_key_refused_meta_included():
    # Unlike callback_attempts' v2, event_attempts has no meta channel at
    # all — ANY extra key, meta included, is a defect.
    assert ea.parse_envelope(b'{"v": 1, "meta": null}') is None
    assert ea.parse_envelope(b'{"v": 1, "meta": "x"}') is None
    assert ea.parse_envelope(b'{"v": 1, "extra": 1}') is None


def test_envelope_wrong_version_refused():
    assert ea.parse_envelope(b'{"v": 2}') is None
    assert ea.parse_envelope(b'{"v": 0}') is None
    assert ea.parse_envelope(b'{"v": true}') is None   # bool is not int 1
    assert ea.parse_envelope(b'{"v": "1"}') is None    # string is not int


def test_envelope_non_dict_refused():
    assert ea.parse_envelope(b"[1, 2]") is None
    assert ea.parse_envelope(b'"hello"') is None
    assert ea.parse_envelope(b"1") is None
    assert ea.parse_envelope(b"null") is None


def test_envelope_size_boundary():
    assert ea.parse_envelope(_envelope_of_size(ea.ENVELOPE_MAX_BYTES + 1)) is None
    assert ea.parse_envelope(_envelope_of_size(ea.ENVELOPE_MAX_BYTES)) is not None


def test_envelope_malformed_refused():
    assert ea.parse_envelope(b"\xff\xfe{}") is None    # not UTF-8
    assert ea.parse_envelope(b"{") is None              # not JSON at all
    assert ea.parse_envelope(b"") is None                # empty


def test_envelope_missing_v_refused():
    assert ea.parse_envelope(b"{}") is None


def test_envelope_rejects_non_finite_json_constants():
    for body in (b'{"v":NaN}', b'{"v":Infinity}', b'{"v":-Infinity}'):
        assert ea.parse_envelope(body) is None, body


def test_envelope_is_total():
    # Never raises, whatever it is fed — including a wrong TYPE entirely
    # (None has no len()/decode()), not just malformed bytes.
    for junk in (None, b"garbage bytes \x00\x01", b"{{{{"):
        assert ea.parse_envelope(junk) is None


# ---------------------------------------------------------------------------
# new_record
# ---------------------------------------------------------------------------

def test_new_record_fields():
    rec = ea.new_record(E, EV, S, GEN, TOK, T)
    assert rec["v"] == ea.SCHEMA_VERSION
    assert rec["emitter"] == E
    assert rec["event"] == EV
    assert rec["subscriber"] == S
    assert rec["gen"] == GEN
    assert rec["ack_token"] == TOK
    assert rec["minted_ts"] == T
    assert rec["status"] == "pending"
    assert rec["outcome"] is None
    assert rec["nudges"] == 0
    assert rec["last_nudge_ts"] is None
    assert rec["next_nudge_ts"] == T + ea.PHASE_OFFSETS[0] == T
    assert rec["deferrals"] == 0
    assert rec["noted"] is False
    assert rec["ended_ts"] is None
    assert set(rec) == {
        "v", "emitter", "event", "subscriber", "gen", "ack_token",
        "minted_ts", "status", "outcome", "nudges", "last_nudge_ts",
        "next_nudge_ts", "deferrals", "noted", "ended_ts",
    }


def test_new_record_is_a_fresh_dict_each_time():
    a = ea.new_record(E, EV, S, GEN, TOK, T)
    b = ea.new_record(E, EV, S, GEN, TOK, T)
    a["nudges"] = 99
    assert b["nudges"] == 0


# ---------------------------------------------------------------------------
# validate_record
# ---------------------------------------------------------------------------

def _good() -> dict:
    return ea.new_record(E, EV, S, GEN, TOK, T)


def test_validate_accepts_json_round_trip():
    rec = _good()
    assert ea.validate_record(json.loads(json.dumps(rec))) == rec
    done = ea.terminalize(rec, "acked", now=T + 5.0)
    assert ea.validate_record(json.loads(json.dumps(done))) == done


def test_validate_rejects_key_set_defects():
    assert ea.validate_record(dict(_good(), bogus=1)) is None
    missing = _good()
    del missing["gen"]
    assert ea.validate_record(missing) is None


def test_validate_rejects_type_defects():
    assert ea.validate_record(dict(_good(), gen=True)) is None       # bool int
    assert ea.validate_record(dict(_good(), gen=-1)) is None         # negative
    assert ea.validate_record(dict(_good(), gen="3")) is None        # not int
    assert ea.validate_record(dict(_good(), nudges=True)) is None
    assert ea.validate_record(dict(_good(), deferrals=-1)) is None
    assert ea.validate_record(dict(_good(), noted=1)) is None        # int bool
    assert ea.validate_record(dict(_good(), emitter=42)) is None
    assert ea.validate_record(dict(_good(), emitter="")) is None     # empty
    assert ea.validate_record(dict(_good(), event="")) is None
    assert ea.validate_record(dict(_good(), subscriber="")) is None
    assert ea.validate_record(dict(_good(), ack_token="")) is None
    assert ea.validate_record(dict(_good(), ack_token=1)) is None
    assert ea.validate_record(dict(_good(), next_nudge_ts=True)) is None
    assert ea.validate_record(dict(_good(), ended_ts="soon")) is None


def test_validate_rejects_wrong_schema_version():
    assert ea.validate_record(dict(_good(), v=2)) is None
    assert ea.validate_record(dict(_good(), v=True)) is None


def test_validate_rejects_unknown_status_and_outcome():
    assert ea.validate_record(dict(_good(), status="weird")) is None
    assert ea.validate_record(dict(_good(), outcome="weird")) is None
    assert ea.validate_record(dict(_good(), outcome="collected")) is None  # not in OUTCOMES


def test_validate_rejects_impossible_status_outcome_pairs():
    assert ea.validate_record(dict(_good(), status="pending", outcome="acked")) is None
    done = ea.terminalize(_good(), "acked", now=T)
    assert ea.validate_record(dict(done, outcome=None)) is None
    assert ea.validate_record(done) == done
    assert ea.validate_record(_good()) == _good()


def test_validate_rejects_none_minted_ts():
    # Unlike callback_attempts' legacy allowance, an event record's minted_ts
    # is always minted from a live clock — None is never legal here.
    assert ea.validate_record(dict(_good(), minted_ts=None)) is None


CLOCK_KEYS = ("last_nudge_ts", "next_nudge_ts", "ended_ts")


def test_validate_rejects_non_finite_and_unbounded_clocks():
    for bad in (float("nan"), float("inf"), float("-inf"),
                10 ** 1000, -(10 ** 1000), ea.TS_ABS_MAX * 2):
        assert ea.validate_record(dict(_good(), minted_ts=bad)) is None, bad
        for key in CLOCK_KEYS:
            assert ea.validate_record(dict(_good(), **{key: bad})) is None, \
                (key, bad)
    # The bound is generous, not pedantic.
    assert ea.validate_record(dict(_good(), minted_ts=ea.TS_ABS_MAX)) is not None


def test_validate_is_total():
    for junk in (None, [], "x", 42, b"{}", {"v": 1}):
        assert ea.validate_record(junk) is None


# ---------------------------------------------------------------------------
# validate_record — local grammar restatement (emitter/event/subscriber are
# spool PATH COMPONENTS, not free text)
# ---------------------------------------------------------------------------

def test_validate_rejects_path_traversal_shaped_identity_fields():
    """Red-case pin (Important finding, task-3 review): emitter/event/
    subscriber become a spool filename (``delivery/<event>--
    <subscriber>.json``) and directory name (``/data/events/<emitter>/``)
    once the spool exists. A charset-only "any non-empty string" check
    would let a scribbled record with subscriber="../../../etc" validate
    today and reach path construction later — this must be refused for
    all three identity fields, not just subscriber."""
    for bad in ("../x", "a/b", "../../../etc", ".", "..", "a b", "UPPER"):
        assert ea.validate_record(dict(_good(), subscriber=bad)) is None, bad
        assert ea.validate_record(dict(_good(), emitter=bad)) is None, bad
    # event has its own (looser, mixed-case) charset but still refuses '/'.
    for bad in ("../x", "a/b", "../../../etc"):
        assert ea.validate_record(dict(_good(), event=bad)) is None, bad


def test_validate_rejects_event_injectivity_rail_violations():
    # Mirrors plugin_events' subscribe-side event rails exactly.
    assert ea.validate_record(dict(_good(), event="a--b")) is None
    assert ea.validate_record(dict(_good(), event="-x")) is None
    assert ea.validate_record(dict(_good(), event="plg-x")) is None


def test_validate_rejects_trailing_dash_event_name():
    """Critical-3 pin: a trailing '-' is just as ambiguous for the
    `<event>--<u32hex>.json` filename split as a leading one — mirrors
    plugin_events' emit/subscribe-side rail."""
    assert ea.validate_record(dict(_good(), event="x-")) is None


def test_validate_accepts_scoped_plugin_identity():
    """P0 pin (mirrors plugin_events' test_scoped_emitter_accepted): a
    bundled/specialist plugin's scoped `slug.manifest_name` identity must
    validate for BOTH emitter and subscriber."""
    scoped = "finance.bank-feed"
    assert ea.validate_record(dict(_good(), emitter=scoped)) is not None
    assert ea.validate_record(dict(_good(), subscriber=scoped)) is not None


def test_validate_expect_identity_binding():
    """Mirrors callback_attempts' expect_hash anti-substitution parameter:
    a record read under one slot's name must not be trusted if its fields
    claim a DIFFERENT slot's identity."""
    good = _good()
    assert ea.validate_record(good, expect_emitter=E, expect_event=EV,
                               expect_subscriber=S) == good
    assert ea.validate_record(good, expect_emitter="other") is None
    assert ea.validate_record(good, expect_event="other-event") is None
    assert ea.validate_record(good, expect_subscriber="other") is None
    # Unbound reads (no expect_* supplied) are unchanged.
    assert ea.validate_record(good) == good


# ---------------------------------------------------------------------------
# terminalize
# ---------------------------------------------------------------------------

def test_terminalize_sets_terminal_fields():
    rec = _good()
    done = ea.terminalize(rec, "acked", now=T + 900.0)
    assert done["status"] == "done"
    assert done["outcome"] == "acked"
    assert done["ended_ts"] == T + 900.0
    assert done["next_nudge_ts"] is None
    # Pure: the input record was not mutated.
    assert rec["status"] == "pending" and rec["ended_ts"] is None


def test_terminalize_double_terminalize_is_a_no_op():
    """Red-case pin: terminal immutability is enforced at the schema layer.
    A SECOND terminalize call — even with a DIFFERENT outcome and a later
    clock — must return the record completely UNCHANGED, not merely
    idempotent-when-the-outcome-matches (callback_attempts' weaker rule)."""
    rec = _good()
    done = ea.terminalize(rec, "acked", now=T + 900.0)
    again_same = ea.terminalize(done, "acked", now=T + 950.0)
    assert again_same == done
    again_different = ea.terminalize(done, "revoked", now=T + 999.0)
    assert again_different == done
    assert again_different["outcome"] == "acked"
    assert again_different["ended_ts"] == T + 900.0


def test_terminalize_unknown_outcome_raises():
    import pytest
    with pytest.raises(ValueError):
        ea.terminalize(_good(), "bogus", now=T)


def test_terminalize_pure_no_input_mutation():
    rec = _good()
    before = json.dumps(rec, sort_keys=True)
    ea.terminalize(rec, "exhausted", now=T)
    assert json.dumps(rec, sort_keys=True) == before


# ---------------------------------------------------------------------------
# nudge ladder
# ---------------------------------------------------------------------------

def _accept(rec: dict, now: float) -> dict:
    """Apply one bus-ACCEPTED dispatch the way the worker will."""
    nxt = ea.next_nudge_after_accept(rec, now=now)
    return dict(rec, nudges=rec["nudges"] + 1, last_nudge_ts=now,
                next_nudge_ts=nxt, deferrals=0)


def test_accept_ladder_walks_all_six_offsets_then_none():
    """Red-case pin: the full 6-accept ladder walk, ending in None once the
    budget of MAX_NUDGES=6 is spent."""
    rec = ea.new_record(E, EV, S, GEN, TOK, T)
    assert rec["next_nudge_ts"] == T  # PHASE_OFFSETS[0] == 0.0
    assert ea.PHASE_OFFSETS == (0.0, 300.0, 1800.0, 7200.0, 21600.0, 86400.0)

    rec = _accept(rec, now=T)
    assert rec["nudges"] == 1 and rec["next_nudge_ts"] == T + 300.0
    rec = _accept(rec, now=T + 300.0)
    assert rec["nudges"] == 2 and rec["next_nudge_ts"] == T + 1800.0
    rec = _accept(rec, now=T + 1800.0)
    assert rec["nudges"] == 3 and rec["next_nudge_ts"] == T + 7200.0
    rec = _accept(rec, now=T + 7200.0)
    assert rec["nudges"] == 4 and rec["next_nudge_ts"] == T + 21600.0
    rec = _accept(rec, now=T + 21600.0)
    assert rec["nudges"] == 5 and rec["next_nudge_ts"] == T + 86400.0
    rec = _accept(rec, now=T + 86400.0)
    assert rec["nudges"] == 6 and rec["next_nudge_ts"] is None  # budget spent
    assert rec["nudges"] == ea.MAX_NUDGES


def test_accept_floors_at_now_never_the_past():
    rec = ea.new_record(E, EV, S, GEN, TOK, T)
    # now is far past the natural anchor slot (e.g. a deferral delayed us).
    late = ea.next_nudge_after_accept(rec, now=T + 10_000.0)
    assert late == T + 10_000.0  # max(now, T + 300.0)


def test_accept_on_terminal_record_is_none():
    rec = ea.new_record(E, EV, S, GEN, TOK, T)
    done = ea.terminalize(rec, "acked", now=T)
    assert ea.next_nudge_after_accept(done, now=T) is None


def test_accept_pure_no_input_mutation():
    rec = ea.new_record(E, EV, S, GEN, TOK, T)
    before = json.dumps(rec, sort_keys=True)
    ea.next_nudge_after_accept(rec, now=T)
    assert json.dumps(rec, sort_keys=True) == before


def test_reject_deferral_doubles_and_caps():
    rec = ea.new_record(E, EV, S, GEN, TOK, T)
    n = 50_000.0
    for deferrals, expect in ((0, 60.0), (1, 120.0), (2, 240.0), (3, 480.0),
                              (4, 960.0), (5, 1800.0), (9, 1800.0)):
        got = ea.next_nudge_after_reject(dict(rec, deferrals=deferrals), now=n)
        assert got == n + expect, (deferrals, got)


def test_reject_spends_no_nudge_budget():
    """Deferral is orthogonal to the nudge ladder: it never touches nudges."""
    rec = ea.new_record(E, EV, S, GEN, TOK, T)
    rec = dict(rec, nudges=2)
    before_nudges = rec["nudges"]
    ea.next_nudge_after_reject(rec, now=T)
    assert rec["nudges"] == before_nudges  # pure; caller doesn't bump nudges


def test_reject_saturates_before_exponentiating():
    rec = ea.new_record(E, EV, S, GEN, TOK, T)
    n = 50_000.0
    for deferrals in (ea.DEFERRAL_MAX_SHIFT, 1024, 10 ** 8):
        got = ea.next_nudge_after_reject(dict(rec, deferrals=deferrals), now=n)
        assert got == n + ea.DEFERRAL_CAP_S, deferrals


def test_reject_malformed_deferrals_reads_as_zero():
    rec = ea.new_record(E, EV, S, GEN, TOK, T)
    for bad in (-1, "x", None, True):
        got = ea.next_nudge_after_reject(dict(rec, deferrals=bad), now=T)
        assert got == T + ea.DEFERRAL_BASE_S, bad


# ---------------------------------------------------------------------------
# gen / ack_token opaque pass-through
# ---------------------------------------------------------------------------

def test_gen_and_ack_token_preserved_by_every_transition():
    rec = ea.new_record(E, EV, S, GEN, TOK, T)
    assert rec["gen"] == GEN and rec["ack_token"] == TOK

    accepted = _accept(rec, now=T)
    assert accepted["gen"] == GEN and accepted["ack_token"] == TOK

    deferred_ts = ea.next_nudge_after_reject(rec, now=T)
    deferred = dict(rec, next_nudge_ts=deferred_ts, deferrals=1)
    assert deferred["gen"] == GEN and deferred["ack_token"] == TOK

    done = ea.terminalize(rec, "revoked", now=T)
    assert done["gen"] == GEN and done["ack_token"] == TOK

    again = ea.terminalize(done, "removed", now=T + 1.0)
    assert again["gen"] == GEN and again["ack_token"] == TOK
