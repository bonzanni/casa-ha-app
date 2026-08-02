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


def _nested_envelope(depth: int) -> bytes:
    """A VALID, well UNDER-cap v2 envelope whose meta is *depth* nested
    arrays — ~1.2 KiB at 600, i.e. nothing a size cap can catch."""
    return b'{"v":2,"meta":' + b"[" * depth + b"]" * depth + b"}"


def test_deeply_nested_meta_never_escapes_as_an_exception():
    """Red case (Sol 4): a ~1.2 KiB envelope of 600 nested arrays parses far
    under ENVELOPE_MAX_BYTES, and every consumer of the parsed value used to
    walk it recursively (``copy.deepcopy``) — a RecursionError escaping into
    the request handler or aborting a whole sweep pass. The parser is TOTAL
    and so is every record built from what it returns: meta is either a
    canonically-serializable value or None, and NOTHING here raises."""
    env = _nested_envelope(600)
    assert len(env) < ca.ENVELOPE_MAX_BYTES

    out = ca.parse_envelope(env)
    assert out is not None and set(out) == {"v", "meta"}

    # Every downstream builder survives the value the parser handed back...
    rec = ca.new_attempt(state_hash=H, minted_ts=T - 5.0,
                         status="result_ready", meta=out["meta"], now=T)
    done = ca.terminalize(rec, "publish_failed", now=T)
    assert done["status"] == "done"
    # ...and so does the round trip through the exact bytes the spool writes.
    for record in (rec, done):
        text = json.dumps(record, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
        assert ca.validate_attempt(json.loads(text)) == record


def test_unserializable_meta_degrades_to_null():
    """A meta the canonical serializer cannot emit is null, never an
    exception a LATER writer raises (spec §4: a malformed envelope degrades
    the binding; refusal buys nothing once the state is consumed)."""
    rec = ca.new_attempt(state_hash=H, minted_ts=None,
                         status="awaiting_redirect", meta={1, 2}, now=T)
    assert rec["meta"] is None


def test_envelope_rejects_non_finite_json_constants():
    """Red case (Sol 3 = Terra 1): JSON has no NaN or Infinity, but Python's
    decoder accepts those literals by default — a 20-byte envelope well under
    the size cap could therefore hand casa a value it can neither compute
    with nor re-emit. The parse fails closed instead, wherever the constant
    sits."""
    for body in (b'{"v":2,"meta":NaN}',
                 b'{"v":2,"meta":Infinity}',
                 b'{"v":2,"meta":-Infinity}',
                 b'{"v":2,"meta":{"deep":[1,NaN]}}',
                 b'{"v":NaN}'):
        assert ca.parse_envelope(body) is None, body
    # The finite neighbour of every one of those still parses.
    assert ca.parse_envelope(b'{"v":2,"meta":{"deep":[1,2.5]}}') is not None


def test_meta_with_a_non_finite_float_degrades_to_null():
    """The serializer half of the same rule: the canonical writer runs with
    `allow_nan=False`, so a non-finite float cannot be written as the
    non-standard `NaN` literal into a record no reader would take back — the
    binding degrades to null, as every other unserializable meta does."""
    rec = ca.new_attempt(state_hash=H, minted_ts=T - 5.0,
                         status="result_ready",
                         meta={"x": float("inf")}, now=T)
    assert rec["meta"] is None
    assert ca.validate_attempt(rec) == rec


def test_envelope_scalar_non_finite_meta_degrades_to_null():
    """Red case (re-review 2): the SCALAR arm of the same rule, and the arm a
    consumer can actually reach without the non-standard literals.

    `1e400` is ordinary JSON — no `NaN` token, so the decoder's
    `parse_constant` hook never fires — and it decodes to `inf`. `_safe_meta`
    handed every scalar straight back, so that `inf` became the flow's
    binding: a value casa can parse and can NEVER re-emit
    (`allow_nan=False`), which every later writer of the record then raises
    on. The proof is the round trip, so a scalar takes it too, and the
    envelope still parses — spec §4: the binding degrades, the flow does
    not refuse."""
    for body in (b'{"v":2,"meta":1e400}', b'{"v":2,"meta":-1e400}',
                 b'{"v":2,"meta":[1e400]}'):
        assert ca.parse_envelope(body) == {"v": 2, "meta": None}, body
    # The finite neighbour is preserved exactly.
    assert ca.parse_envelope(b'{"v":2,"meta":1e30}') == {"v": 2, "meta": 1e30}


def test_new_attempt_scalar_non_finite_meta_degrades_to_null():
    """The in-process half of the same hole: `meta` also arrives from a
    result record casa reads back off disk (`_derive_from_artifacts`), whose
    marker reader accepts the `NaN` literal the canonical writer refuses. A
    scalar non-finite value must never become a record's binding."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        rec = ca.new_attempt(state_hash=H, minted_ts=T - 5.0,
                             status="result_ready", meta=bad, now=T)
        assert rec["meta"] is None, bad
        assert ca.validate_attempt(rec) == rec, bad
        # terminalize agrees: the record it copies is writable too.
        done = ca.terminalize(dict(rec, meta=bad), "expired", now=T)
        assert done["meta"] is None, bad
        assert ca.validate_attempt(done) == done, bad


def test_new_attempt_unusable_mint_clock_degrades_to_null():
    """The same shape one field over: `minted_ts` is read off an artifact —
    an `st_mtime`, or the `minted_ts` transport key of a result record a
    scribbler can reach — and a type-only check admits `NaN` and `10**1000`
    alike. Either would build a record the canonical writer cannot emit AT
    ALL, so the flow's write-ahead outcome could never go durable and the
    artifact it authorizes would never be deleted. What `new_attempt`
    returns always validates."""
    for bad in (float("nan"), float("inf"), 10 ** 1000, ca.TS_ABS_MAX * 2):
        rec = ca.new_attempt(state_hash=H, minted_ts=bad,
                             status="result_ready", now=T)
        assert rec["minted_ts"] is None, bad
        assert ca.validate_attempt(rec) == rec, bad
    # An ordinary mint clock is untouched.
    assert ca.new_attempt(state_hash=H, minted_ts=T - 5.0,
                          status="result_ready", now=T)["minted_ts"] == T - 5.0


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


def test_validate_rejects_impossible_status_outcome_pairs():
    """Red case (Terra 2): a record whose fields all type-check can still be
    IMPOSSIBLE, and `list_attempts` would hand it to the worker as truth.
    Outcome is set for exactly `done` — in both directions."""
    # An open record wearing a terminal outcome.
    assert ca.validate_attempt(
        dict(_good(), status="result_ready", outcome="collected")) is None
    assert ca.validate_attempt(
        dict(_good(), status="awaiting_redirect", outcome="expired")) is None
    # A terminal record with no outcome to act on.
    done = ca.terminalize(_good(), "collected", now=T)
    assert ca.validate_attempt(dict(done, outcome=None)) is None
    # Both consistent forms still validate.
    assert ca.validate_attempt(done) == done
    assert ca.validate_attempt(_good()) == _good()


def test_validate_rejects_a_state_hash_that_is_not_the_name_grammar():
    """Red case (Terra 2): `state_hash` is a spool NAME; a record naming
    something that cannot be a flow is not a record."""
    for bad in ("", "not-a-hash", H.upper(), H[:-1], H + "a", H[:-1] + "g"):
        assert ca.validate_attempt(dict(_good(), state_hash=bad)) is None, bad
    assert ca.validate_attempt(_good()) is not None


CLOCK_KEYS = ("minted_ts", "last_nudge_ts", "next_nudge_ts", "ended_ts")


def test_validate_rejects_non_finite_and_unbounded_clocks():
    """Red case (Sol 3 = Terra 1): every clock field of a validated record is
    COMPUTED with, so "a number that is not a bool" is not enough.

    * `NaN` makes every comparison False: a `next_nudge_ts` of NaN is never
      "in the future", so the nudge is due on every pass until the budget is
      burnt, and an `ended_ts` of NaN is never older than the retention bound,
      so the record never retires;
    * `±Infinity` is the same class from the other side;
    * `10**1000` type-checks as an int and then OverflowErrors the first float
      it meets — the outcome-phase `ended_ts + offset` after each accepted
      dispatch — so the budget update never lands and the worker can dispatch
      indefinitely.

    All of them read as INVALID, which is what makes the caller re-derive the
    record from the artifacts."""
    for bad in (float("nan"), float("inf"), float("-inf"),
                10 ** 1000, -(10 ** 1000), ca.TS_ABS_MAX * 2):
        for key in CLOCK_KEYS:
            assert ca.validate_attempt(dict(_good(), **{key: bad})) is None, \
                (key, bad)
    done = ca.terminalize(_good(), "expired_unread", now=T)
    for bad in (float("nan"), 10 ** 1000):
        assert ca.validate_attempt(dict(done, ended_ts=bad)) is None, bad
    # The bound is generous, not pedantic: ordinary clocks still validate,
    # and the schedule arithmetic on what survives is plain finite float.
    survivor = ca.validate_attempt(dict(done, ended_ts=T))
    assert survivor is not None
    assert ca.next_nudge_after_accept(survivor, now=T) == T + 7200.0


def test_validate_rejects_a_stored_meta_the_writer_cannot_emit():
    """Red case (re-review 2): a STORED record is authoritative — the worker
    nudges from it and the write-ahead outcome is derived from it — and every
    write of it goes out through the `allow_nan=False` canonical serializer.
    A record carrying a value that serializer REFUSES is therefore a record
    whose updates all fail: an accepted dispatch whose `nudges`/
    `next_nudge_ts` never advance leaves the attempt due on every pass, which
    is INV-CB-008's bounded redelivery broken through the one field a
    consumer authors.

    So here — unlike a MINT, where the state is already consumed and §4
    degrades the binding rather than refusing the flow — the record reads as
    INVALID, and the caller re-derives it from the live artifacts."""
    for bad in (float("nan"), float("inf"), float("-inf"),      # scalars
                {"deep": [1, float("nan")]}, [float("inf")],    # nested
                {1, 2}):                                        # unencodable
        assert ca.validate_attempt(dict(_good(), meta=bad)) is None, bad
    # Everything the writer CAN emit still validates, meta null included.
    for good in (None, 0, "", False, 1e308, {"k": [1, 2.5]}):
        assert ca.validate_attempt(dict(_good(), meta=good)) is not None, good


def test_validate_binds_the_record_to_the_name_it_was_read_under():
    """Red case (Sol 4): `state_hash` obeying the name GRAMMAR is not the same
    as it being THIS flow's record. `attempts/<A>.json` containing
    `state_hash: B` would otherwise carry B's identity (and B's `meta`)
    through A's write-ahead outcome and onto the consumer's read surface — so
    every authoritative read passes the name it read the file under, and a
    mismatch is INVALID, i.e. re-derived."""
    other = "cd" * 32
    assert ca.validate_attempt(_good(), expect_hash=H) == _good()
    assert ca.validate_attempt(_good(), expect_hash=other) is None
    assert ca.validate_attempt(dict(_good(), state_hash=other),
                               expect_hash=H) is None
    # Unbound reads are unchanged (the grammar gate still applies).
    assert ca.validate_attempt(dict(_good(), state_hash=other)) is not None
    assert ca.validate_attempt(dict(_good(), state_hash="nope"),
                               expect_hash="nope") is None


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

def _accept(rec: dict, now: float, anchor_ts: float | None = None) -> dict:
    """Apply one bus-ACCEPTED dispatch the way the worker will."""
    nxt = ca.next_nudge_after_accept(rec, now=now, anchor_ts=anchor_ts)
    return dict(rec, nudges=rec["nudges"] + 1, last_nudge_ts=now,
                next_nudge_ts=nxt, deferrals=0)


def test_accept_schedule_walks_offsets_then_outcome_phase():
    # anchor = the result file's mtime (its publish time), worker-supplied.
    rec = ca.new_attempt(state_hash=H, minted_ts=None,
                         status="result_ready", now=T)
    assert rec["next_nudge_ts"] == T                       # +0 from publish
    rec = _accept(rec, now=T, anchor_ts=T)
    assert rec["next_nudge_ts"] == T + 60.0                # +60, absolute
    rec = _accept(rec, now=T + 60.0, anchor_ts=T)
    assert rec["next_nudge_ts"] == T + 180.0               # +3 m, absolute
    rec = _accept(rec, now=T + 180.0, anchor_ts=T)
    assert rec["next_nudge_ts"] == T + 480.0               # +8 m, absolute
    rec = _accept(rec, now=T + 480.0, anchor_ts=T)
    assert rec["next_nudge_ts"] is None                    # result phase spent

    ended = T + 900.0                                      # sweep terminalizes
    rec = ca.terminalize(rec, "expired_unread", now=ended)
    assert rec["next_nudge_ts"] == ended + 1800.0          # +30 m from ended_ts
    rec = _accept(rec, now=ended + 1800.0)
    assert rec["next_nudge_ts"] == ended + 7200.0          # +2 h from ended_ts
    rec = _accept(rec, now=ended + 7200.0)
    assert rec["next_nudge_ts"] is None                    # budget of 6 spent
    assert rec["nudges"] == ca.MAX_NUDGES


def test_accept_after_deferral_returns_to_anchor_cadence():
    # A rejected-dispatch deferral moves next_nudge_ts off-grid; the next
    # accepted dispatch must land back on the ABSOLUTE anchor cadence
    # (anchor + 60/180/480), floored at now — never position-relative drift.
    anchor = T
    rec = ca.new_attempt(state_hash=H, minted_ts=None,
                         status="result_ready", now=T)   # slot: anchor + 0
    deferred = ca.next_nudge_after_reject(rec, now=T + 5.0)
    assert deferred == T + 65.0                          # 60 * 2**0 deferral
    rec = dict(rec, next_nudge_ts=deferred, deferrals=1)

    # Accept before the +60 slot: back on the anchor cadence exactly.
    early = ca.next_nudge_after_accept(rec, now=T + 30.0, anchor_ts=anchor)
    assert early == anchor + 60.0
    # Accept after the +60 slot has passed: floored at now, never the past.
    late = ca.next_nudge_after_accept(rec, now=T + 65.0, anchor_ts=anchor)
    assert late == T + 65.0
    # Fallback (anchor unavailable mid-pass): position-relative advance.
    fallback = ca.next_nudge_after_accept(rec, now=T + 65.0, anchor_ts=None)
    assert fallback == deferred + 60.0


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


def test_reject_saturates_before_exponentiating():
    """Red case (Sol 5): `deferrals` comes off a file a consumer can
    scribble, and the validator's "non-negative int" admits enormous values.
    Computing `2 ** deferrals` BEFORE the cap turns one of those into a
    memory/CPU bomb — and overflows the float multiply long before that
    (1024 already raises OverflowError). The cap is reached at 5 deferrals,
    so saturating is exact, and the worker never exponentiates."""
    rec = ca.new_attempt(state_hash=H, minted_ts=None,
                         status="result_ready", now=T)
    n = 50_000.0
    for deferrals in (ca.DEFERRAL_MAX_SHIFT, 1024, 10 ** 8):
        got = ca.next_nudge_after_reject(dict(rec, deferrals=deferrals), now=n)
        assert got == n + ca.DEFERRAL_CAP_S, deferrals
