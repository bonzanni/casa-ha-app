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
  ``meta`` is opaque: stored value-preserving, never interpreted, never
  logged (INV-CB-009 is the caller's obligation; nothing here logs).
- **Nudge schedule**: result-phase offsets (0 s, 60 s, 3 m, 8 m) anchored
  on the result's publish time, then outcome-phase offsets (30 m, 2 h)
  anchored on ``ended_ts``; a total budget of 6 bus-ACCEPTED dispatches;
  an escalating capped deferral for all-rejected passes.
"""
from __future__ import annotations

import copy
import json

SCHEMA_VERSION = 1
ENVELOPE_MAX_BYTES = 4096
MAX_NUDGES = 6
RESULT_PHASE_OFFSETS = (0.0, 60.0, 180.0, 480.0)   # from result publish ts
OUTCOME_PHASE_OFFSETS = (1800.0, 7200.0)           # from ended_ts
DEFERRAL_BASE_S = 60.0
DEFERRAL_CAP_S = 1800.0
ATTEMPT_RETENTION_S = 7 * 24 * 3600
OUTCOMES = frozenset({"collected", "expired", "expired_unread",
                      "publish_failed", "evicted"})
STATUSES = ("awaiting_redirect", "result_ready", "done")

_ATTEMPT_KEYS = frozenset({
    "v", "state_hash", "minted_ts", "status", "outcome", "claimed", "meta",
    "nudges", "last_nudge_ts", "next_nudge_ts", "deferrals", "noted",
    "ended_ts",
})


def _is_ts(value) -> bool:
    """A timestamp field: numeric-not-bool, or None."""
    return value is None or (
        isinstance(value, (int, float)) and not isinstance(value, bool))


def parse_envelope(data: bytes) -> dict | None:
    """Bounded fail-closed envelope parse: ``None`` on ANY defect.

    Defects: over ``ENVELOPE_MAX_BYTES``, non-UTF-8, non-JSON, non-object
    JSON, ``v`` not in {1, 2}. A v1 envelope (or a v2 one with no ``meta``)
    yields ``{"v": <v>, "meta": None}``. Unknown envelope keys are dropped
    (forward compat) and never copied anywhere.
    """
    try:
        if len(data) > ENVELOPE_MAX_BYTES:
            return None
        obj = json.loads(data.decode("utf-8"))
        if not isinstance(obj, dict):
            return None
        v = obj.get("v")
        if isinstance(v, bool) or v not in (1, 2):
            return None
        meta = obj.get("meta") if v == 2 else None
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
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    next_ts = now + RESULT_PHASE_OFFSETS[0] if status == "result_ready" else None
    return {
        "v": SCHEMA_VERSION,
        "state_hash": state_hash,
        "minted_ts": minted_ts,
        "status": status,
        "outcome": None,
        "claimed": bool(claimed),
        "meta": copy.deepcopy(meta),
        "nudges": 0,
        "last_nudge_ts": None,
        "next_nudge_ts": next_ts,
        "deferrals": 0,
        "noted": False,
        "ended_ts": None,
    }


def validate_attempt(obj) -> dict | None:
    """Total fail-closed validation: a copy of ``obj``, or ``None``.

    ``None`` unless ``obj`` is a dict with exactly the schema keys and
    every field type-checks: real bools for the flags (bool-typed ints
    rejected), numeric-not-bool-or-None timestamps (``minted_ts`` None is
    legal — legacy/collect-held records), status/outcome in their
    vocabularies, non-negative int counters. Never raises: a malformed
    (possibly consumer-scribbled) file must read as INVALID so the caller
    re-derives it from artifacts, never as an exception.
    """
    try:
        if not isinstance(obj, dict) or set(obj) != _ATTEMPT_KEYS:
            return None
        if isinstance(obj["v"], bool) or obj["v"] != SCHEMA_VERSION:
            return None
        if not isinstance(obj["state_hash"], str):
            return None
        if obj["status"] not in STATUSES:
            return None
        if obj["outcome"] is not None and obj["outcome"] not in OUTCOMES:
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
        return copy.deepcopy(obj)
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
    out = copy.deepcopy(rec)
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


def next_nudge_after_accept(rec: dict, *, now: float) -> float | None:
    """The next ``next_nudge_ts`` after a bus-ACCEPTED dispatch.

    The accept consumes one budget unit (the caller writes ``nudges + 1``);
    ``None`` when that spends the budget or the record is a terminal
    ``collected``. Result-phase anchors on the current schedule position:
    the next offset's delta is added to the slot just dispatched, so a
    deferral-shifted schedule keeps its spacing. Outcome-phase anchors on
    ``ended_ts``: the next slot strictly after the one just dispatched.
    ``None`` also when a phase's offsets are exhausted — an open attempt
    that spent the result-phase slots waits for terminalization to start
    the outcome phase.
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
    return current + (RESULT_PHASE_OFFSETS[idx + 1] - RESULT_PHASE_OFFSETS[idx])


def next_nudge_after_reject(rec: dict, *, now: float) -> float:
    """The deferred ``next_nudge_ts`` after an all-rejected dispatch pass.

    ``now + min(DEFERRAL_CAP_S, DEFERRAL_BASE_S * 2**deferrals)`` — the
    escalating capped deferral (the caller writes ``deferrals + 1``), so an
    unavailable bus yields a bounded cadence, never a floored-timeout spin.
    A malformed ``deferrals`` reads as 0.
    """
    deferrals = rec.get("deferrals")
    if isinstance(deferrals, bool) or not isinstance(deferrals, int) or deferrals < 0:
        deferrals = 0
    return now + min(DEFERRAL_CAP_S, DEFERRAL_BASE_S * 2 ** deferrals)
