"""Canonical argument JSON + the single-use, TTL-bound authorization
GrantStore (A:§3.3, §3.6).

This module is Part 1 of 3 for the ``ask_user`` + authorization-grants
project (v0.76.0). Later tasks EXTEND it — Task 5 adds a
``ChallengeCoordinator`` (async settlement, two latches) and Task 7 adds
``make_resident_authz_hook``/``AuthzDeps`` and ``protected_map``
consumption. Keep this file leaf-level: no imports of ``agent``/``tools``
at module scope, new sections appended below rather than interleaved, and
the ``GRANTS`` singleton kept at the very bottom of its section so later
tasks can import it without reaching into internals.

Sections, top to bottom:

1. Canonical JSON + hash (§3.6) — pure functions used both to compute the
   ``GrantKey.args_hash`` and to render the challenge message body.
2. ``GrantKey`` + ``GrantStore`` (§3.3) — a thread-safe, single-use,
   TTL-bound store of "operator approved this exact tool call" grants.
   ``GRANTS`` is the process-wide singleton.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

# ---------------------------------------------------------------------------
# Canonical JSON (A:§3.6)
# ---------------------------------------------------------------------------


def canonical_args_json(tool_input: dict) -> str:
    """Render *tool_input* as canonical JSON.

    ``sort_keys=True`` gives a stable key order (recursively, for every
    nested dict), ``separators=(",", ":")`` drops the default whitespace,
    ``ensure_ascii=False`` leaves unicode unescaped, and ``allow_nan=False``
    rejects ``nan``/``inf``/``-inf`` — these cannot be rendered faithfully
    to an operator confirming a tool call, so they must never silently
    round-trip through a hash.

    Raises ``ValueError`` if *tool_input* contains a non-finite float
    (``allow_nan=False`` makes ``json.dumps`` raise ``ValueError`` for
    those) or any value ``json.dumps`` cannot serialize at all (a
    ``TypeError`` is re-raised as ``ValueError`` with a clearer message,
    so callers only need to catch one exception type).
    """
    try:
        return json.dumps(
            tool_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except TypeError as exc:
        raise ValueError(f"tool_input is not JSON-serializable: {exc}") from exc


def canonical_args_hash(tool_input: dict) -> str:
    """sha256 hexdigest of ``canonical_args_json(tool_input)`` (UTF-8)."""
    return hashlib.sha256(
        canonical_args_json(tool_input).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# GrantKey + GrantStore (A:§3.3)
# ---------------------------------------------------------------------------

# Grant TTL default, per the plan's Global Constraints: "grant TTL 300 s
# default; single-use".
DEFAULT_GRANT_TTL_S = 300.0


@dataclass(frozen=True)
class GrantKey:
    """Identifies exactly one operator-approved tool call.

    ``artifact_id`` binds the grant to the resolved plugin artifact at
    challenge time — a plugin update mid-TTL changes the artifact and
    invalidates the grant. ``enforcement_role`` is the plain role (never
    tier-qualified) that is actually allowed to consume it.
    """

    operator_id: int
    chat_id: int
    enforcement_role: str
    artifact_id: str
    tool_name: str
    args_hash: str


@dataclass
class _Grant:
    """Internal record: when it expires, and whether it has been used."""

    expires_at: float
    used: bool = False


class GrantStore:
    """Thread-safe, single-use, TTL-bound store of operator-approved
    tool-call grants.

    A ``threading.Lock`` guards every mutation — not ``asyncio.Lock`` —
    because the SDK PreToolUse hook's thread-context relative to the
    event loop is uncertain (pinned in the plan as a load-bearing
    assumption); a real-thread concurrency test proves ``consume`` is
    genuinely serialized, not just coroutine-safe.

    ``_now`` is an injectable zero-arg clock (default ``time.monotonic``)
    so tests can drive TTL expiry deterministically without sleeping.
    """

    def __init__(self, *, _now: Callable[[], float] = time.monotonic) -> None:
        self._now = _now
        self._lock = threading.Lock()
        self._grants: dict[GrantKey, _Grant] = {}

    def mint(self, key: GrantKey, *, ttl_s: float = DEFAULT_GRANT_TTL_S) -> None:
        """Create a fresh, unused grant for *key*, replacing any live one.

        Minting always wins over whatever was there before — a stale
        unused grant, an already-consumed one, or an expired one are all
        simply overwritten with a new record.
        """
        grant = _Grant(expires_at=self._now() + ttl_s)
        with self._lock:
            self._grants[key] = grant

    def consume(self, key: GrantKey) -> bool:
        """Atomically mark *key* used and return ``True`` — exactly once.

        Returns ``False`` when no grant exists for *key*, it already
        expired, or it was already consumed. The whole check-and-mark
        happens under the lock, so N callers racing the SAME key see
        exactly one ``True``.
        """
        with self._lock:
            grant = self._grants.get(key)
            if grant is None:
                return False
            if grant.used or grant.expires_at <= self._now():
                return False
            grant.used = True
            return True

    def purge_chat(self, chat_id: int) -> int:
        """Drop every grant for *chat_id*. Returns the count removed."""
        return self._purge(lambda k: k.chat_id == chat_id)

    def purge_role(self, role: str) -> int:
        """Drop every grant whose ``enforcement_role`` is *role*."""
        return self._purge(lambda k: k.enforcement_role == role)

    def purge_artifact(self, artifact_id: str) -> int:
        """Drop every grant whose ``artifact_id`` is *artifact_id*."""
        return self._purge(lambda k: k.artifact_id == artifact_id)

    def sweep(self) -> int:
        """Drop every grant whose TTL has passed. Returns the count
        removed. Used grants that have not yet expired are left alone —
        this is a pure TTL sweep, not a used-grant reaper."""
        now = self._now()
        return self._purge(lambda k: self._grants[k].expires_at <= now)

    def _purge(self, predicate: Callable[[GrantKey], bool]) -> int:
        with self._lock:
            matched = [k for k in self._grants if predicate(k)]
            for k in matched:
                del self._grants[k]
            return len(matched)


# Process-wide singleton. Later tasks (the ask_user tool's /new purge, the
# PreToolUse authz hook's consume, plugin/reload lifecycle purges, and the
# casa_core.py hourly sweep job) all import THIS instance.
GRANTS = GrantStore()
