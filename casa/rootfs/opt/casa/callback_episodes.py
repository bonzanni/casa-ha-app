"""At-least-once delivery nudge for authorization callbacks (v0.146.0, spec §7).

An authorization code dies in 30–600 s; a pull-only pickup with a long TTL is
a live-looking corpse. So when a result lands in the spool
(:mod:`callback_spool`), casa enqueues a **durable "callback received"
episode** — modeled directly on :mod:`plugin_setup_episodes` (crash-safe
recording, boot recovery, a supervised worker) — that dispatches a fixed
**casa-authored** turn to the plugin's assigned role:

    Authorization result for '<plugin>' is waiting (handle <hash>) —
    collect it now.

The turn is internal and **system-attributed** (the ``synthetic`` context
marker, mirroring ``plugin_setup_episodes``/``_setup_dispatch``), so it needs
no ingress-identity row; ``<hash>`` is the non-secret result handle
(``sha256(state)``, already the result's filename) so a successor session can
find and collect it. Target selection copies
``plugin_setup_episodes._compose`` **verbatim**: ``resident:assistant`` when
targeted, else the lexicographically-first resident, else the first specialist
via assistant delegation.

**State model.** The ledger is keyed ``(plugin, result_hash)``:

* ``pending``  — enqueued, the nudge not yet accepted by the bus.
* ``dispatched`` — the bus accepted the turn. A **consumed-key tombstone** is
  written *atomically with this mark*: that tombstone is the durable record
  that makes redelivery **at-least-once** — its absence after a crash between
  bus-accept and the mark re-enqueues the flow (a second nudge; idempotent for
  the consumer, whose "check your results" against an emptied dir collects
  nothing).
* *settled* — a dispatched episode whose result file has become **absent or
  expired**. Its row AND its tombstone are pruned together (Sol r3: a
  tombstone is **retained until its result is gone** — pruning it earlier would
  let the recovery scan re-enqueue a still-lingering result). A later result
  reusing the same hash therefore re-enqueues cleanly.

**Request-path discipline.** :func:`kick` is O(1) and touches no file: it
records an in-memory hint and signals the worker. The worker does every
durable write (``_load``/``_save``) off the request path. Durability comes
from the **recovery invariant**, not the kick: :func:`recovery` (boot +
periodic) enqueues an episode for any result lacking an episode/tombstone, so
a lost kick converges rather than dropping the flow.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

STORE_PATH = Path("/data/callback-episodes.json")
_SCHEMA_VERSION = 1
_MAX_DISPATCH_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 5.0)

# Wired by casa_core at boot (Task 8). All optional — absent seams degrade to
# logging, exactly like plugin_setup_episodes.
_dispatch: Callable[[str, str, dict], Awaitable[bool]] | None = None
_notify_operator: Callable[[str], Awaitable[None]] | None = None
_resolve_registry_entry: Callable[[str], Any] | None = None
_get_spool: Callable[[], Any] | None = None
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

_lock: asyncio.Lock | None = None
_worker_task: asyncio.Task | None = None
_kick: asyncio.Event | None = None

# In-memory, non-durable request-path hint set: kick() appends here (O(1)) and
# the worker drains it. Correctness never depends on it — the recovery scan is
# the backstop — so a hint lost to a crash converges on the next pass.
_pending_hints: set[tuple[str, str]] = set()


def _now() -> float:
    return time.time()


def configure(*, dispatch, resolve_registry_entry, get_spool,
              notify_operator=None, sleep=asyncio.sleep) -> None:
    """casa_core boot wiring (Task 8). Idempotent. ``get_spool()`` returns the
    process-wide :class:`callback_spool.CallbackSpool` (or ``None`` before boot
    wired it); the worker reads results through it. ``resolve_registry_entry
    (plugin)`` returns an overlay entry ``{"targets": [...]}`` (or ``None`` when
    the plugin cannot be resolved yet — the episode then stays pending and
    retries)."""
    global _dispatch, _notify_operator, _resolve_registry_entry
    global _get_spool, _sleep, _lock, _kick
    _dispatch = dispatch
    _notify_operator = notify_operator
    _resolve_registry_entry = resolve_registry_entry
    _get_spool = get_spool
    _sleep = sleep
    if _lock is None:
        _lock = asyncio.Lock()
    if _kick is None:
        _kick = asyncio.Event()


# ---------------------------------------------------------------------------
# Store: {"schema_version", "episodes": [...], "tombstones": [...]}
# ---------------------------------------------------------------------------

def _empty() -> dict:
    return {"schema_version": _SCHEMA_VERSION, "episodes": [],
            "tombstones": []}


def _load() -> dict:
    if not STORE_PATH.is_file():
        return _empty()
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if (not isinstance(data, dict)
                or not isinstance(data.get("episodes"), list)):
            raise ValueError("malformed store")
        data.setdefault("tombstones", [])
        if not isinstance(data["tombstones"], list):
            data["tombstones"] = []
        return data
    except Exception:  # noqa: BLE001 — a corrupt store must not brick boot
        logger.exception("callback-episodes store unreadable — resetting")
        return _empty()


def _save(data: dict) -> None:
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, STORE_PATH)


def episodes(status: str | None = None) -> list[dict]:
    eps = _load()["episodes"]
    return [e for e in eps if status is None or e.get("status") == status]


# ---------------------------------------------------------------------------
# kick — O(1), non-durable, no file I/O on the request path
# ---------------------------------------------------------------------------

def kick(plugin: str, result_hash: str) -> None:
    """Signal the worker that a result landed. Records a non-durable in-memory
    hint and sets the wake event — no ``_load``/``_save`` — so the HTTP
    handler's per-request work stays O(1). The worker records durably; the
    recovery invariant covers a lost hint."""
    _pending_hints.add((plugin, result_hash))
    if _kick is not None:
        _kick.set()


# ---------------------------------------------------------------------------
# ledger operations (caller holds the lock / is yield-free)
# ---------------------------------------------------------------------------

def _has_key(data: dict, plugin: str, result_hash: str) -> bool:
    """True if an episode OR a tombstone already owns this key — the idempotence
    gate for enqueue."""
    if any(e.get("plugin") == plugin and e.get("result_hash") == result_hash
           for e in data["episodes"]):
        return True
    return any(t.get("plugin") == plugin and t.get("result_hash") == result_hash
               for t in data["tombstones"])


def _enqueue_locked(data: dict, plugin: str, result_hash: str) -> bool:
    """Append a ``pending`` episode unless the key is already an episode or a
    tombstone (idempotent). Returns True when one was created."""
    if _has_key(data, plugin, result_hash):
        return False
    data["episodes"].append({
        "id": uuid.uuid4().hex[:12],
        "plugin": plugin,
        "result_hash": result_hash,
        "status": "pending",
        "attempts": 0,
        "created_ts": _now(),
        "updated_ts": _now(),
    })
    return True


def _reconcile_locked(data: dict, spool: Any) -> None:
    """Enqueue-and-prune against the live spool (caller holds the lock):

    * drain the in-memory kick hints and the full result listing into
      idempotent enqueues;
    * settle — drop any episode whose result file is **absent** (a dispatched
      episode's result was collected/expired; a pending episode's credential
      died before the nudge fired — either way there is nothing left to
      collect), and prune tombstones the same way so a later same-hash result
      re-enqueues.
    """
    hints = list(_pending_hints)
    _pending_hints.clear()

    if spool is None:
        # No spool wired yet: enqueue the hints optimistically; the next pass
        # with a live spool reconciles (drops any that never had a result).
        for plugin, h in hints:
            _enqueue_locked(data, plugin, h)
        return

    for plugin, h in hints:
        if spool.has_result(plugin, h):
            _enqueue_locked(data, plugin, h)
    for plugin in spool.plugins():
        for h in spool.list_results(plugin):
            _enqueue_locked(data, plugin, h)

    data["episodes"] = [
        e for e in data["episodes"]
        if spool.has_result(e.get("plugin"), e.get("result_hash"))]
    data["tombstones"] = [
        t for t in data["tombstones"]
        if spool.has_result(t.get("plugin"), t.get("result_hash"))]


def _mark_dispatched(episode_id: str, plugin: str, result_hash: str) -> None:
    """The durable at-least-once boundary: set the episode ``dispatched`` and
    write its consumed-key tombstone in ONE ``_save``. A crash before this
    leaves the episode ``pending`` (no tombstone) so the flow redelivers."""
    data = _load()
    for e in data["episodes"]:
        if e.get("id") == episode_id:
            e.update(status="dispatched", updated_ts=_now())
            break
    if not _any_tombstone(data, plugin, result_hash):
        data["tombstones"].append({
            "plugin": plugin, "result_hash": result_hash, "ts": _now()})
    _save(data)


def _any_tombstone(data: dict, plugin: str, result_hash: str) -> bool:
    return any(t.get("plugin") == plugin
               and t.get("result_hash") == result_hash
               for t in data["tombstones"])


def _update_episode(episode_id: str, **fields) -> None:
    data = _load()
    for e in data["episodes"]:
        if e.get("id") == episode_id:
            e.update(fields, updated_ts=_now())
            break
    _save(data)


# ---------------------------------------------------------------------------
# target selection + fixed message (verbatim from plugin_setup_episodes._compose)
# ---------------------------------------------------------------------------

def _compose(entry: dict, plugin: str, result_hash: str) -> tuple[str | None, str]:
    """Deterministic execution-target selection + the fixed casa-authored
    nudge. Returns ``(role, instruction)`` or ``(None, reason)``.

    Target order copies ``plugin_setup_episodes._compose`` verbatim:
    ``resident:assistant`` when targeted; else the lexicographically first
    resident; else the first specialist via assistant delegation (the
    specialist has no channel — the instruction names the EXACT specialist and
    forbids substitution)."""
    targets = entry.get("targets") or []
    residents = sorted(t.split(":", 1)[1] for t in targets
                       if t.startswith("resident:"))
    specialists = sorted(t.split(":", 1)[1] for t in targets
                         if t.startswith("specialist:"))
    base = (f"Authorization result for '{plugin}' is waiting "
            f"(handle {result_hash}) — collect it now.")
    if "assistant" in residents:
        return "assistant", base
    if residents:
        return residents[0], base
    if specialists:
        sp = specialists[0]
        return "assistant", (
            f"Delegate to the specialist '{sp}' with the instruction: {base} "
            "Do not substitute another agent.")
    return None, "no resident or specialist target"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def start_worker() -> None:
    """Boot seam (Task 8): start the supervised nudge worker. It reconciles
    against the spool first (boot recovery of results without an episode), then
    dispatches ``pending`` episodes."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.get_running_loop().create_task(
        _worker(), name="callback-episodes")
    if _kick is not None:
        _kick.set()


async def recovery(spool: Any) -> None:
    """Boot/periodic recovery pass (spec §7): enqueue an episode for any result
    lacking an episode/tombstone, and settle any whose result is gone. Never
    dispatches — that is the worker's job — so casa_core can run it before the
    worker starts. Never raises."""
    if _lock is None:
        return
    try:
        async with _lock:
            data = _load()
            _reconcile_locked(data, spool)
            _save(data)
    except Exception:  # noqa: BLE001
        logger.exception("callback-episodes recovery pass failed")
        return
    if _kick is not None:
        _kick.set()


async def _worker_pass() -> None:
    """One drain pass: reconcile against the spool, then dispatch pending
    episodes. Each episode is isolated so one failure never strands the rest."""
    spool = _get_spool() if _get_spool is not None else None
    if _lock is not None:
        async with _lock:
            data = _load()
            _reconcile_locked(data, spool)
            _save(data)
    for ep in episodes("pending"):
        try:
            await _run_episode(ep)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — isolate per episode
            logger.exception("callback episode %s failed unexpectedly",
                             ep.get("id"))


async def _worker() -> None:
    while True:
        try:
            assert _kick is not None
            await _kick.wait()
            _kick.clear()
            await _worker_pass()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the worker must survive anything
            logger.exception("callback-episodes worker pass failed")
            await _sleep(5.0)
            if _kick is not None:
                _kick.set()  # self re-kick: never strand pending episodes


async def _run_episode(ep: dict) -> None:
    """Dispatch one pending episode's nudge. Unresolvable registry ⇒ stay
    pending (transient — retried on a later kick); no target ⇒ fail + note;
    bus accept ⇒ the durable dispatched+tombstone mark."""
    plugin = ep["plugin"]
    result_hash = ep["result_hash"]
    entry = _resolve(plugin)
    if entry is None:
        _update_episode(ep["id"], last_error="registry not resolved yet")
        return
    role, instruction = _compose(entry, plugin, result_hash)
    if role is None:
        _update_episode(ep["id"], status="failed", last_error=instruction)
        await _note(f"Plugin {plugin}: an authorization result is waiting but "
                    f"the delivery nudge could not run ({instruction}). Ask the "
                    "agent to collect it manually.")
        return
    ok = False
    # Per-PASS attempt budget (not carried into the loop bound): a transient
    # bus outage that exhausts this pass leaves the episode pending, and the
    # next periodic/boot kick retries afresh while the result still exists.
    tries = 0
    while tries < _MAX_DISPATCH_ATTEMPTS and not ok:
        tries += 1
        if _dispatch is not None:
            try:
                ok = await _dispatch(role, instruction, {
                    "synthetic": "callback_nudge",
                    "callback_episode": ep["id"],
                })
            except Exception:  # noqa: BLE001
                logger.exception("callback episode %s: dispatch raised",
                                 ep["id"])
                ok = False
        if not ok and tries < _MAX_DISPATCH_ATTEMPTS:
            await _sleep(_RETRY_BACKOFF_S[min(tries - 1,
                                              len(_RETRY_BACKOFF_S) - 1)])
    total = int(ep.get("attempts") or 0) + tries
    if ok:
        _mark_dispatched(ep["id"], plugin, result_hash)
    else:
        # Left PENDING (never tombstoned): the credential is short-lived, so
        # the next periodic/boot kick retries while the result still exists —
        # the reconcile pass drops it once the result is gone.
        _update_episode(ep["id"], attempts=total,
                        last_error="dispatch not accepted")


def _resolve(plugin: str) -> dict | None:
    """Resolve the plugin's overlay entry. ``None`` (resolver absent, raised,
    or returned a non-dict) means "cannot resolve yet" — the caller RETAINS the
    pending episode and retries; it must never treat this as a confirmed
    removal."""
    if _resolve_registry_entry is None:
        return None
    try:
        entry = _resolve_registry_entry(plugin)
    except Exception:  # noqa: BLE001
        logger.exception("callback-episodes registry resolve failed (plugin=%s)",
                         plugin)
        return None
    return entry if isinstance(entry, dict) else None


async def _note(text: str) -> None:
    if _notify_operator is None:
        return
    try:
        await _notify_operator(text)
    except Exception:  # noqa: BLE001
        logger.exception("callback-episode operator note failed")
