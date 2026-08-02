"""Authorization-attempt bookkeeping — pure schema, envelope, and schedule
logic.

Leaf module (stdlib only). The callback facility's per-flow attempt ledger
lives at ``/data/callbacks/<plugin>/attempts/<state_hash>.json`` — casa's
durable record of every minted authorization flow AND the consumer's read
surface. This module is the calculation half of that ledger: it builds,
validates, terminalizes, and schedules attempt records. It performs no
I/O — ``callback_spool.py`` owns every read/write (marker discipline,
atomic replace, the spool lock); everything here is a pure function that
returns fresh copies and never mutates its inputs.

Three concerns, matching the design's sections:

- **Attempt schema**: the exact 13-key record (``new_attempt``), and a
  total fail-closed validator (``validate_attempt``) — a malformed file is
  never trusted; the caller re-derives it from live artifacts.
- **Mint envelope**: bounded fail-closed parsing of the consumer-authored
  pending payload (``parse_envelope``) — v1 ``{"v":1}`` and v2
  ``{"v":2,"meta":...}`` accepted, anything else degrades to ``None``.
  ``meta`` is opaque: stored value-preserving (a copy through the canonical
  serializer, which is also the proof that every later writer can emit it —
  a consumer-authored value must never escape as an exception), never
  interpreted, never logged (INV-CB-009 is the caller's obligation; nothing
  here logs).
- **Nudge schedule**: result-phase offsets (0 s, 60 s, 3 m, 8 m) anchored
  on the result's publish time, then outcome-phase offsets (30 m, 2 h)
  anchored on ``ended_ts``; a total budget of 6 bus-ACCEPTED dispatches;
  an escalating capped deferral for all-rejected passes.
"""
from __future__ import annotations

import json
import math
import re

SCHEMA_VERSION = 1
ENVELOPE_MAX_BYTES = 4096
MAX_NUDGES = 6
RESULT_PHASE_OFFSETS = (0.0, 60.0, 180.0, 480.0)   # from result publish ts
OUTCOME_PHASE_OFFSETS = (1800.0, 7200.0)           # from ended_ts
DEFERRAL_BASE_S = 60.0
DEFERRAL_CAP_S = 1800.0
#: Deferral count past which the schedule SATURATES without exponentiating.
#: ``deferrals`` is read off a file a consumer can scribble, and
#: ``2 ** deferrals`` for a large-but-valid integer is a memory/CPU bomb
#: (and overflows the float multiply long before that). The cap is reached
#: at 5 deferrals with the constants above, so saturating here is exact, not
#: an approximation.
DEFERRAL_MAX_SHIFT = 32
ATTEMPT_RETENTION_S = 7 * 24 * 3600
#: Absolute bound, in epoch seconds, on every clock field of a VALID record.
#: A validated record is ARITHMETIC-SAFE by contract: the schedule adds nudge
#: offsets to ``ended_ts``, the retention bound subtracts ``ended_ts`` from
#: ``now``, and the worker compares ``next_nudge_ts`` against a clock. An
#: unbounded integer defeats that while type-checking perfectly — ``10**1000``
#: is a "number", and the first float it meets raises ``OverflowError`` (an
#: accepted dispatch whose budget update then never lands is a worker that can
#: dispatch indefinitely). Roughly thirty million years of headroom costs
#: nothing and keeps every such expression a plain, finite float.
TS_ABS_MAX = 1e15
OUTCOMES = frozenset({"collected", "expired", "expired_unread",
                      "publish_failed", "evicted"})
STATUSES = ("awaiting_redirect", "result_ready", "done")

_ATTEMPT_KEYS = frozenset({
    "v", "state_hash", "minted_ts", "status", "outcome", "claimed", "meta",
    "nudges", "last_nudge_ts", "next_nudge_ts", "deferrals", "noted",
    "ended_ts",
})
#: The spool's name grammar for a state hash, re-stated here because this is
#: a LEAF module (``callback_spool`` imports it, never the reverse).
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_ts(value) -> bool:
    """A usable timestamp field: ``None``, or a FINITE, arithmetic-safe
    number that is not a bool.

    Mirrors ``callback_acks._valid_ts`` and then goes one step further,
    because a validated attempt record is *computed with*. Point by point:

    * a ``bool`` is an ``int`` subclass and is never a clock;
    * an ``int`` is never NaN/inf, so it needs no ``math.isfinite`` — and
      must not be given one: ``isfinite(10**1000)`` raises ``OverflowError``
      converting to a C double, and a validator that raises is a validator
      that crashes a sweep instead of failing a file closed;
    * a ``float`` goes through ``math.isfinite``, which is what rejects the
      NaN that would make ``NaN > now`` False forever (a nudge due on every
      pass) and the ``NaN`` ``ended_ts`` that would make the retention age
      comparison False forever (a record that never retires);
    * both are then bounded by :data:`TS_ABS_MAX`. The comparison is exact
      for an ``int`` of any size (CPython compares int-to-float without
      converting), so the bound itself can never raise.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -TS_ABS_MAX <= value <= TS_ABS_MAX
    if isinstance(value, float):
        return math.isfinite(value) and -TS_ABS_MAX <= value <= TS_ABS_MAX
    return False


def _reject_constant(name: str):
    """``json``'s hook for its NON-STANDARD ``NaN`` / ``Infinity`` /
    ``-Infinity`` literals. JSON has no such values; Python's decoder accepts
    them by default, which would let a consumer-authored envelope smuggle a
    non-finite float past a size cap and into a record. Raising here makes
    the parse fail closed."""
    raise ValueError(f"non-finite JSON constant {name!r}")


def _canonical_text(value) -> str:
    """*value* in the ONE canonical serialization the spool writes on disk
    (``callback_spool.canonical_marker_bytes``, kept in sync by construction:
    same sort/separators/escaping/``allow_nan``). Raises exactly what that
    writer would — including on a non-finite float, which ``allow_nan=False``
    refuses rather than emitting the non-standard ``NaN`` literal that no
    conforming JSON reader (and no fail-closed validator) would take back."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _safe_meta(value):
    """A canonically-serializable copy of the OPAQUE consumer value *value*,
    or ``None`` when there is none — or when *value* is not one.

    ``meta`` is the only field of an attempt that a consumer authors, and it
    is arbitrary JSON: a ~1 KiB body of 600 nested arrays parses well under
    ``ENVELOPE_MAX_BYTES`` yet blows the interpreter stack in any recursive
    Python walk (``copy.deepcopy``, which this replaces). Copying through the
    canonical serializer is therefore both the copy AND the proof that every
    later handler of the record — the strict attempt write, the result
    record, a re-read and re-validate — can serialize it too. TOTAL: a value
    that cannot survive the round trip degrades to ``None`` (spec §4's
    "malformed envelope degrades to meta null; refusal buys nothing"), never
    an exception escaping into a request handler or aborting a sweep pass.

    **Every value takes that round trip, SCALARS INCLUDED.** A scalar is
    immutable and can neither alias nor recurse, so short-circuiting it looks
    free — but the round trip is not only a copy, it is the serializability
    PROOF, and ``float('nan')`` (or ``±inf``) is precisely a scalar that
    fails it. Handing one back unexamined lets a non-finite meta type-check
    its way into a record whose every subsequent write the ``allow_nan=False``
    canonical writer then refuses: a bus-ACCEPTED nudge whose budget update
    never lands leaves the attempt due forever, and INV-CB-008's bounded
    redelivery is broken through the one field a consumer authors."""
    if value is None:
        return None
    try:
        return json.loads(_canonical_text(value))
    except (RecursionError, ValueError, TypeError):
        return None


def _writable_meta(value) -> bool:
    """True iff the canonical writer can EMIT *value* — the predicate half of
    :func:`_safe_meta`, for the one caller that must REFUSE such a value
    rather than quietly drop it (:func:`validate_attempt`)."""
    try:
        _canonical_text(value)
    except (RecursionError, ValueError, TypeError):
        return False
    return True


def _copy_record(rec: dict) -> dict:
    """A fresh copy of an attempt record, non-recursive by construction: the
    twelve schema fields besides ``meta`` are scalars (a shallow ``dict`` copy
    is a full copy of them), and ``meta`` — the one arbitrary-depth field —
    goes through :func:`_safe_meta`. Replaces ``copy.deepcopy``, whose
    recursion a consumer-authored ``meta`` can exhaust."""
    out = dict(rec)
    out["meta"] = _safe_meta(rec.get("meta"))
    return out


def parse_envelope(data: bytes) -> dict | None:
    """Bounded fail-closed envelope parse: ``None`` on ANY defect.

    Defects: over ``ENVELOPE_MAX_BYTES``, non-UTF-8, non-JSON, non-object
    JSON, a non-finite JSON constant anywhere in the body (``NaN`` /
    ``Infinity`` / ``-Infinity`` — Python's decoder accepts those extensions
    by default, and a value casa cannot re-emit must not enter a record),
    ``v`` not in {1, 2}. A v1 envelope (or a v2 one with no ``meta``)
    yields ``{"v": <v>, "meta": None}``. Unknown envelope keys are dropped
    (forward compat) and never copied anywhere.

    ``meta`` additionally passes :func:`_safe_meta`, so what leaves here is
    always a value the canonical serializer can write: a value casa could
    parse but not re-emit would otherwise escape as an exception from a
    LATER handler (the attempt write, the result record) rather than as this
    parser's ``None``. The envelope itself still parses — a meta defect
    degrades the binding to null, it does not refuse the flow (spec §4).
    """
    try:
        if len(data) > ENVELOPE_MAX_BYTES:
            return None
        obj = json.loads(data.decode("utf-8"),
                         parse_constant=_reject_constant)
        if not isinstance(obj, dict):
            return None
        v = obj.get("v")
        if isinstance(v, bool) or v not in (1, 2):
            return None
        meta = _safe_meta(obj.get("meta")) if v == 2 else None
        return {"v": v, "meta": meta}
    except Exception:
        return None


def new_attempt(*, state_hash: str, minted_ts: float | None,
                status: str, meta=None, claimed: bool = False,
                now: float) -> dict:
    """A fresh attempt record with all schema fields.

    ``next_nudge_ts`` is computed only for a ``result_ready`` status
    (``now + RESULT_PHASE_OFFSETS[0]`` — the publish kick); an
    ``awaiting_redirect`` attempt has none (nudges exist only once a
    result or terminal outcome exists). ``minted_ts`` may be None when no
    source for the mint clock survives (collect-held materialization).

    Both consumer-reachable inputs are proved here, so what this returns
    always VALIDATES: ``meta`` through :func:`_safe_meta`, and ``minted_ts``
    through :func:`_is_ts` — the mint clock is read off an artifact
    (``st_mtime``, or a ``minted_ts`` transport key from a result record a
    scribbler can reach), and a NaN or unbounded one would build a record the
    canonical writer refuses to emit at all: the flow's write-ahead outcome
    could then never go durable, so the artifact it authorizes would never be
    deleted. An unusable clock degrades to None, exactly like an unusable
    ``meta``.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    next_ts = now + RESULT_PHASE_OFFSETS[0] if status == "result_ready" else None
    return {
        "v": SCHEMA_VERSION,
        "state_hash": state_hash,
        "minted_ts": minted_ts if _is_ts(minted_ts) else None,
        "status": status,
        "outcome": None,
        "claimed": bool(claimed),
        "meta": _safe_meta(meta),
        "nudges": 0,
        "last_nudge_ts": None,
        "next_nudge_ts": next_ts,
        "deferrals": 0,
        "noted": False,
        "ended_ts": None,
    }


def validate_attempt(obj, *, expect_hash: str | None = None) -> dict | None:
    """Total fail-closed validation: a copy of ``obj``, or ``None``.

    ``None`` unless ``obj`` is a dict with exactly the schema keys and
    every field type-checks: real bools for the flags (bool-typed ints
    rejected), finite arithmetic-safe timestamps (:func:`_is_ts`;
    ``minted_ts`` None is legal — legacy/collect-held records),
    status/outcome in their vocabularies, non-negative int counters. Never
    raises: a malformed (possibly consumer-scribbled) file must read as
    INVALID so the caller re-derives it from artifacts, never as an
    exception.

    Type-checking each field in isolation is not enough, because the record
    this returns becomes AUTHORITATIVE (``list_attempts``, the write-ahead
    derivation): a record must also be internally POSSIBLE. Three
    consistency gates, all fail-closed:

    * ``state_hash`` obeys the spool's name grammar (64 lowercase hex) — a
      record naming something that cannot be a flow is not a record;
    * ``state_hash`` EQUALS *expect_hash* when the caller supplies one. Every
      authoritative read of an attempt comes from a file whose NAME is the
      flow's identity, and a grammar check alone lets ``attempts/<A>.json``
      contain ``state_hash: B`` — carrying B's identity through A's
      write-ahead outcome and onto the consumer's read surface. Bound here
      rather than at each call site so no reader can forget; a mismatch is
      INVALID, so the caller re-derives A's record from A's artifacts;
    * status and outcome agree, in both directions: ``outcome`` is None for
      exactly the open statuses and set for exactly ``done``. Otherwise
      ``{status: result_ready, outcome: collected}`` (an open record wearing
      a terminal outcome) and ``{status: done, outcome: null}`` (a terminal
      record with no outcome to act on) would both read as truth;
    * ``meta`` is a value the canonical writer can EMIT. Here alone the rule
      is refusal rather than :func:`_safe_meta`'s degradation, and the two
      are not in tension: a MINT degrades (the state is consumed, the flow
      must complete), while a STORED record that cannot be re-emitted must
      never be authoritative — every update of it (the write-ahead outcome,
      the nudge bookkeeping) goes out through ``allow_nan=False`` and would
      fail, leaving an accepted dispatch whose budget never advances, i.e. a
      flow redelivered on every pass. INVALID sends it to the re-derivation
      worklist instead, which rebuilds it from live artifacts — with the
      consumer's real ``meta``, since the mint envelope's own parse already
      rejects the non-finite constants (:func:`parse_envelope`).
    """
    try:
        if not isinstance(obj, dict) or set(obj) != _ATTEMPT_KEYS:
            return None
        if isinstance(obj["v"], bool) or obj["v"] != SCHEMA_VERSION:
            return None
        if not isinstance(obj["state_hash"], str) \
                or not _HASH_RE.match(obj["state_hash"]):
            return None
        if expect_hash is not None and obj["state_hash"] != expect_hash:
            return None
        if obj["status"] not in STATUSES:
            return None
        if obj["outcome"] is not None and obj["outcome"] not in OUTCOMES:
            return None
        if (obj["outcome"] in OUTCOMES) != (obj["status"] == "done"):
            return None
        for key in ("claimed", "noted"):
            if not isinstance(obj[key], bool):
                return None
        for key in ("nudges", "deferrals"):
            n = obj[key]
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                return None
        for key in ("minted_ts", "last_nudge_ts", "next_nudge_ts", "ended_ts"):
            if not _is_ts(obj[key]):
                return None
        if not _writable_meta(obj["meta"]):
            return None
        return _copy_record(obj)
    except Exception:
        return None


def terminalize(rec: dict, outcome: str, *, now: float,
                claimed: bool | None = None) -> dict:
    """A copy of ``rec`` made terminal: ``done``/``outcome``, ``ended_ts=now``.

    ``next_nudge_ts`` becomes ``now + OUTCOME_PHASE_OFFSETS[0]`` when the
    outcome is not ``collected`` AND nudge budget remains, else ``None``
    (``collected`` never nudges). ``claimed`` overrides the record's flag
    when not None. Idempotent: terminalizing an already-done record with
    the same outcome returns an equal dict (``ended_ts`` and the schedule
    are NOT re-stamped), so outcome rewrites before a retried deletion
    cannot drift the schedule.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    out = _copy_record(rec)
    if claimed is not None:
        out["claimed"] = bool(claimed)
    if rec.get("status") == "done" and rec.get("outcome") == outcome:
        return out
    out["status"] = "done"
    out["outcome"] = outcome
    out["ended_ts"] = now
    if outcome != "collected" and out["nudges"] < MAX_NUDGES:
        out["next_nudge_ts"] = now + OUTCOME_PHASE_OFFSETS[0]
    else:
        out["next_nudge_ts"] = None
    return out


def next_nudge_after_accept(rec: dict, *, now: float,
                            anchor_ts: float | None = None) -> float | None:
    """The next ``next_nudge_ts`` after a bus-ACCEPTED dispatch.

    The accept consumes one budget unit (the caller writes ``nudges + 1``);
    ``None`` when that spends the budget or the record is a terminal
    ``collected``. Result-phase anchors on ``anchor_ts`` — the result
    file's mtime (its publish time), supplied by the worker — so the
    absolute (+0, +60, +180, +480) cadence is kept even after a
    rejected-dispatch deferral moved ``next_nudge_ts`` off-grid; the slot
    is floored at ``now`` (never scheduled in the past). When ``anchor_ts``
    is None (result already gone mid-pass) the fallback advances by the
    next offset's delta from the slot just dispatched. Outcome-phase
    anchors on ``ended_ts``: the next slot strictly after the one just
    dispatched. ``None`` also when a phase's offsets are exhausted — an
    open attempt that spent the result-phase slots waits for
    terminalization to start the outcome phase.
    """
    if rec.get("status") == "done" and rec.get("outcome") == "collected":
        return None
    if rec["nudges"] + 1 >= MAX_NUDGES:
        return None
    current = rec.get("next_nudge_ts")
    if current is None:
        # Nothing was scheduled; an accept cannot invent a schedule.
        return None
    if rec.get("status") == "done":
        for offset in OUTCOME_PHASE_OFFSETS:
            slot = rec["ended_ts"] + offset
            if slot > current:
                return slot
        return None
    idx = rec["nudges"]  # the result-phase position just dispatched
    if idx + 1 >= len(RESULT_PHASE_OFFSETS):
        return None
    if anchor_ts is not None:
        return max(now, anchor_ts + RESULT_PHASE_OFFSETS[idx + 1])
    return current + (RESULT_PHASE_OFFSETS[idx + 1] - RESULT_PHASE_OFFSETS[idx])


def next_nudge_after_reject(rec: dict, *, now: float) -> float:
    """The deferred ``next_nudge_ts`` after an all-rejected dispatch pass.

    ``now + min(DEFERRAL_CAP_S, DEFERRAL_BASE_S * 2**deferrals)`` — the
    escalating capped deferral (the caller writes ``deferrals + 1``), so an
    unavailable bus yields a bounded cadence, never a floored-timeout spin.
    A malformed ``deferrals`` reads as 0.

    **Saturate BEFORE exponentiating.** ``deferrals`` is read off a file a
    consumer can scribble, and the validator's "non-negative int" is
    satisfied by ``10**9`` — for which ``2 ** deferrals`` is a 125 MB
    integer (and ``60.0 *`` it an ``OverflowError``) long before the ``min``
    would have discarded it. Past ``DEFERRAL_MAX_SHIFT`` the answer IS the
    cap, so it is returned without computing the power.
    """
    deferrals = rec.get("deferrals")
    if isinstance(deferrals, bool) or not isinstance(deferrals, int) or deferrals < 0:
        deferrals = 0
    if deferrals >= DEFERRAL_MAX_SHIFT:
        return now + DEFERRAL_CAP_S
    return now + min(DEFERRAL_CAP_S, DEFERRAL_BASE_S * 2 ** deferrals)
