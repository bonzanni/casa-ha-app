"""Durable post-consent setup episodes (v0.112.0, casa-plugin-elevenlabs#2).

A plugin that declares ``casa.setupTool`` gets its setup tool run
AUTOMATICALLY once its trigger-consent round settles with EVERY prompted
trigger approved — the operator's Approve taps are the authorization for
the wiring that makes the triggers functional. Because plugin MCP tools
surface only on the plugin's target agents, Casa dispatches a synthetic
Casa-authored turn to the execution agent rather than calling the tool
itself.

Design (Sol+Terra design round + implementation rounds 1-3, 2026-07-24):

* **Round ledger, not ack counting**: every prompted consent registers an
  OPEN member with a fresh per-prompt NONCE (:func:`register_prompt`);
  terminal decisions mark members. Settlement = ALL members decided.
* **All-approved gate** (impl r3): the episode dispatches only when every
  member is APPROVED. Any denial settles the round WITHOUT a dispatch and
  tells the operator — the argument-free setup tool cannot distinguish
  approved from denied triggers, so a mixed round must not wire blindly.
* **Crash-safe approval recording** (impl r3): approvals are recorded
  SYNCHRONOUSLY inside the consent ack commit callback
  (:func:`record_approval_sync` — same yield-free step that persists the
  ack), and a BOOT RECOVERY SWEEP (:func:`_boot_recover`, first act of the
  worker) marks any still-open member whose identity has a persisted ack
  as approved with that ack's generation — a crash anywhere between ack
  persistence and settlement recovers on restart.
* **Prompt nonces** (impl r3): denials/expiries apply only when their
  nonce matches the member's CURRENT nonce — a late expiry callback from a
  superseded keyboard (re-prompt of the same identity) cannot decide the
  fresh prompt. Approvals are exempt: the persisted ack is ground truth.
* **Stale-artifact fencing** (impl r2): the decision path never replaces
  an existing round with a different artifact_id — only the prompt path
  (which runs solely from a live reconcile) starts a new-artifact round.
* **Re-consent mints a new episode**: approvals carry ``identity#gen``
  (the ack's approval generation) in the episode key.
* **Consumed-key tombstones** (impl r2): supersession keeps pruned
  episodes' keys (bounded) so a replayed stale generation can never
  recreate a consumed episode.
* **Exact-artifact binding (TOCTOU)**: the worker re-resolves the registry
  at dispatch time and marks the episode ``stale`` when the plugin was
  removed or superseded. (The residual dispatch→agent-turn window is an
  ACCEPTED, disclosed risk: seconds wide, and the tool is idempotent
  wiring whose current-artifact consent round re-runs setup regardless.)
* **Unambiguous tool binding**: exactly one server grant or the episode
  fails with a clear reason; verify blocks ambiguous plugins upstream.
* **Worker survivability**: per-episode isolation + self re-kick.
* **Terminal-state hygiene**: supersession prunes a plugin's older
  episodes; ``failed``/``stale`` decay out of health after 72h.
* **Delivery semantics (disclosed)**: ``dispatched`` means the turn was
  accepted by the in-process bus and the target agent will report the
  actual outcome to the operator — the durable retry contract covers
  consent-to-dispatch, not tool execution. User-facing claims are worded
  accordingly.
* **No plugin prose**: fixed Casa-authored template; only grammar-
  validated identifiers interpolated. The ``synthetic`` marker is a
  RESERVED provenance key external ingress cannot spoof.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

STORE_PATH = Path("/data/plugin-setup-episodes.json")
_SCHEMA_VERSION = 3
_MAX_DISPATCH_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 5.0)
_HEALTH_DECAY_S = 72 * 3600.0
_TOMBSTONE_CAP = 50

# Wired by casa_core at boot. All optional — absent seams degrade to logging.
_dispatch: Callable[[str, str, dict], Awaitable[bool]] | None = None
_notify_operator: Callable[[str], Awaitable[None]] | None = None
_resolve_registry_entry: Callable[[str], Any] | None = None
_ack_lookup: Callable[[str], str | None] | None = None
_routes_live: Callable[[str], bool] | None = None
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

_lock: asyncio.Lock | None = None
_worker_task: asyncio.Task | None = None
_kick: asyncio.Event | None = None


def _now() -> float:
    return time.time()


def configure(*, dispatch, notify_operator, resolve_registry_entry,
              ack_lookup=None, routes_live=None,
              sleep=asyncio.sleep) -> None:
    """casa_core boot wiring. Idempotent. ``ack_lookup(identity)`` returns
    the persisted ack's approval generation (or None) — the boot recovery
    sweep's ground truth. ``routes_live(plugin)`` reports whether the
    plugin's triggers are ROUTED (impl r4, Sol): the worker holds a pending
    episode until the route overlay is live, so the external service is
    never pointed at an unrouted endpoint; reconciles call :func:`kick` to
    retry the gate."""
    global _dispatch, _notify_operator, _resolve_registry_entry
    global _ack_lookup, _routes_live, _sleep, _lock, _kick
    _dispatch = dispatch
    _notify_operator = notify_operator
    _resolve_registry_entry = resolve_registry_entry
    _ack_lookup = ack_lookup
    _routes_live = routes_live
    _sleep = sleep
    if _lock is None:
        _lock = asyncio.Lock()
    if _kick is None:
        _kick = asyncio.Event()


# ---------------------------------------------------------------------------
# Store: {"schema_version", "rounds": {plugin: {"artifact_id", "members":
#   {identity: {"state", "gen", "nonce"}}}}, "episodes": [...],
#   "consumed_keys": [...]}
# ---------------------------------------------------------------------------

def _load() -> dict:
    if not STORE_PATH.is_file():
        return {"schema_version": _SCHEMA_VERSION, "rounds": {},
                "episodes": [], "consumed_keys": []}
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if (not isinstance(data, dict)
                or not isinstance(data.get("episodes"), list)):
            raise ValueError("malformed store")
        data.setdefault("rounds", {})
        data.setdefault("consumed_keys", [])
        return data
    except Exception:  # noqa: BLE001 — a corrupt store must not brick boot
        logger.exception("plugin-setup-episodes store unreadable — resetting")
        return {"schema_version": _SCHEMA_VERSION, "rounds": {},
                "episodes": [], "consumed_keys": []}


def _save(data: dict) -> None:
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, STORE_PATH)


def episodes(status: str | None = None) -> list[dict]:
    eps = _load()["episodes"]
    return [e for e in eps if status is None or e.get("status") == status]


def health_issues() -> list[dict]:
    """Non-terminal-success episodes for plugin-health regeneration.
    ``failed``/``stale`` rows decay after :data:`_HEALTH_DECAY_S`;
    ``pending`` never decays (actionable until dispatched)."""
    out = []
    cutoff = _now() - _HEALTH_DECAY_S
    for e in episodes():
        st = e.get("status")
        if st == "pending" or (
                st in ("failed", "stale")
                and float(e.get("updated_ts") or 0) >= cutoff):
            out.append({
                "kind": f"setup_episode_{st}",
                "plugin": e.get("plugin"),
                "episode": e.get("id"),
                "detail": e.get("last_error") or "",
            })
    return out


def _episode_key(plugin: str, artifact_id: str,
                 approved_keys: list[str]) -> str:
    h = hashlib.sha256()
    h.update(plugin.encode())
    h.update(artifact_id.encode())
    for ident in sorted(approved_keys):
        h.update(ident.encode())
    return h.hexdigest()[:24]


# ---------------------------------------------------------------------------
# Round ledger
# ---------------------------------------------------------------------------

def open_round(*, plugin: str, artifact_id: str,
               identities: list[str]) -> dict[str, str]:
    """SEAL a consent round's membership BEFORE any keyboard posts (impl
    r4): the reconciler declares the COMPLETE per-plugin batch it is about
    to prompt in ONE yield-free call, so a fast Approve on the first
    keyboard can never settle a round that is still registering its other
    members. Returns ``{identity: nonce}`` — the caller threads each nonce
    into that keyboard's decision callbacks (stale-expiry fencing).

    Merges into an existing same-artifact round: listed members are
    (re)opened with fresh nonces, other members keep their states — a
    later reconcile batch that re-prompts a subset must not erase earlier
    decisions. A different artifact starts a fresh round. SYNCHRONOUS +
    yield-free — cannot interleave with the locked async sections. Never
    raises (returns {} on failure: unfenced but never incorrect)."""
    try:
        data = _load()
        rnd = data["rounds"].get(plugin)
        if not isinstance(rnd, dict) or rnd.get("artifact_id") != artifact_id:
            rnd = {"artifact_id": artifact_id, "members": {}}
            data["rounds"][plugin] = rnd
        nonces: dict[str, str] = {}
        for identity in identities:
            existing = rnd["members"].get(identity)
            # impl r5 (Terra): a member that is ALREADY open keeps its nonce.
            # A reconcile re-firing a prompt while its keyboard is still live
            # DEDUPES onto that keyboard (coordinator.register_challenge
            # returns created=False, retaining the ORIGINAL finish callback
            # and its nonce) — minting a fresh nonce here would desync the
            # ledger from that retained callback, so the keyboard's eventual
            # deny/expiry would be rejected as stale and the member would
            # never decide. A NEW member, or one being RE-OPENED after a
            # terminal decision (its old keyboard is gone, a fresh keyboard
            # with our new callback+nonce posts), gets a fresh nonce.
            if isinstance(existing, dict) and existing.get("state") == "open" \
                    and existing.get("nonce"):
                nonces[identity] = existing["nonce"]
                continue
            nonce = uuid.uuid4().hex[:8]
            rnd["members"][identity] = {"state": "open", "nonce": nonce}
            nonces[identity] = nonce
        _save(data)
        return nonces
    except Exception:  # noqa: BLE001 — prompt path must never see a raise
        logger.exception("setup-round open failed (plugin=%s)", plugin)
        return {}


def kick() -> None:
    """Wake the worker (episode created elsewhere, or a reconcile just ran
    and may have made a pending episode's routes live)."""
    if _kick is not None:
        _kick.set()


def record_approval_sync(*, plugin: str, artifact_id: str, identity: str,
                         gen: str) -> None:
    """Record an approval DURABLY in the same yield-free commit step that
    persists the consent ack (impl r3: a crash after the ack but before the
    async finish hook must not strand the round — this write happens
    first; the boot sweep covers a crash even earlier). Approvals are
    ack-backed ground truth, so no nonce check applies. Never raises."""
    try:
        data = _load()
        rnd = data["rounds"].get(plugin)
        if isinstance(rnd, dict) and rnd.get("artifact_id") != artifact_id:
            logger.info("stale approval record ignored (plugin=%s)", plugin)
            return
        if not isinstance(rnd, dict):
            rnd = {"artifact_id": artifact_id, "members": {}}
            data["rounds"][plugin] = rnd
        member = rnd["members"].get(identity) or {}
        member.update({"state": "approved", "gen": gen})
        rnd["members"][identity] = member
        _save(data)
    except Exception:  # noqa: BLE001 — commit callback must never see a raise
        logger.exception("sync approval record failed (plugin=%s)", plugin)


async def on_consent_decision(*, plugin: str, artifact_id: str,
                              identity: str, approved: bool,
                              approval_gen: str = "",
                              nonce: str = "") -> None:
    """Feed ONE terminal consent decision. Approvals re-apply idempotently
    (the sync record already ran) and then settle; denials/expiries apply
    ONLY when ``nonce`` matches the member's current nonce (a superseded
    keyboard's late callback is ignored). Settlement runs under ONE lock
    acquisition; notes/kick happen after release. Never raises."""
    if _lock is None:
        return
    notes: list[str] = []
    created = False
    try:
        async with _lock:
            data = _load()
            rnd = data["rounds"].get(plugin)
            if isinstance(rnd, dict) and rnd.get("artifact_id") != artifact_id:
                logger.info(
                    "stale consent decision ignored (plugin=%s artifact=%s, "
                    "current round is %s)", plugin, artifact_id,
                    rnd.get("artifact_id"))
                return
            if not isinstance(rnd, dict):
                # Unknown round (e.g. store reset) — synthesize so a live
                # decision is never dropped.
                rnd = {"artifact_id": artifact_id, "members": {}}
                data["rounds"][plugin] = rnd
            member = rnd["members"].get(identity)
            if approved:
                m = member or {}
                m["state"] = "approved"
                # impl r4 (Terra): never overwrite a durably-recorded
                # generation with a blank from a feed whose acks.get failed.
                if approval_gen or not m.get("gen"):
                    m["gen"] = approval_gen
                rnd["members"][identity] = m
            else:
                # Nonce fence (impl r3): a deny/expiry from a SUPERSEDED
                # keyboard (mismatching nonce) must not decide the current
                # prompt. Fencing needs both sides to carry a nonce — a
                # blank on either side degrades to unfenced acceptance.
                cur_nonce = (member or {}).get("nonce") or ""
                if (member is not None and member.get("state") == "open"
                        and nonce and cur_nonce and nonce != cur_nonce):
                    logger.info(
                        "stale deny/expiry ignored (plugin=%s identity "
                        "nonce mismatch)", plugin)
                    _save(data)
                    return
                m = member or {}
                if m.get("state") != "approved":  # an ack outranks an expiry
                    m["state"] = "denied"
                rnd["members"][identity] = m
            created, notes = _settle_locked(data, plugin)
            _save(data)
    except Exception:  # noqa: BLE001 — consent flow must never see a raise
        logger.exception("setup-episode decision handling failed (plugin=%s)",
                         plugin)
        return
    for n in notes:
        await _note(n)
    if created and _kick is not None:
        _kick.set()


def _settle_locked(data: dict, plugin: str) -> tuple[bool, list[str]]:
    """Settlement body (caller holds the lock / is yield-free): all members
    decided ⇒ consume the round; ALL approved ⇒ claim an episode; any
    denial ⇒ operator note, no dispatch. Returns (episode_created, notes).
    Mutates ``data`` (caller saves)."""
    rnd = data["rounds"].get(plugin)
    if not isinstance(rnd, dict):
        return False, []
    members = rnd.get("members") or {}
    if not members or any(m.get("state") == "open" for m in members.values()):
        return False, []
    del data["rounds"][plugin]
    denied = [i for i, m in members.items() if m.get("state") == "denied"]
    if denied:
        return False, [
            f"Plugin {plugin}: consent settled with "
            f"{len(denied)} unapproved trigger(s) — its setup tool was NOT "
            "run automatically (it cannot target a subset). Run it manually "
            "if intended."]
    approved_keys = sorted(f"{i}#{m.get('gen', '')}"
                           for i, m in members.items())
    entry = None
    if _resolve_registry_entry is not None:
        try:
            entry = _resolve_registry_entry(plugin)
        except Exception:  # noqa: BLE001
            logger.exception("registry resolve failed (plugin=%s)", plugin)
    setup = (entry or {}).get("setup_tool") if isinstance(entry, dict) else None
    if not setup:
        return False, []
    artifact_id = rnd.get("artifact_id") or ""
    key = _episode_key(plugin, artifact_id, approved_keys)
    consumed = data.setdefault("consumed_keys", [])
    if any(e.get("key") == key for e in data["episodes"]) or key in consumed:
        return False, []
    for old in data["episodes"]:
        if old.get("plugin") == plugin and old.get("key"):
            consumed.append(old["key"])
    del consumed[:-_TOMBSTONE_CAP]
    data["episodes"] = [e for e in data["episodes"]
                        if e.get("plugin") != plugin]
    data["episodes"].append({
        "id": uuid.uuid4().hex[:12],
        "key": key,
        "plugin": plugin,
        "artifact_id": artifact_id,
        "setup_tool": setup,
        "approved_identities": approved_keys,
        "status": "pending",
        "attempts": 0,
        "created_ts": _now(),
        "updated_ts": _now(),
    })
    return True, []


async def _boot_recover() -> None:
    """First act of the worker (impl r3): recover rounds stranded by a
    crash between ack persistence and decision recording — any OPEN member
    whose identity has a persisted ack becomes approved with that ack's
    generation, then settlement re-runs per plugin."""
    if _lock is None or _ack_lookup is None:
        return
    notes: list[str] = []
    created_any = False
    try:
        async with _lock:
            data = _load()
            for plugin in list(data["rounds"].keys()):
                rnd = data["rounds"][plugin]
                for identity, m in (rnd.get("members") or {}).items():
                    if m.get("state") != "open":
                        continue
                    try:
                        gen = _ack_lookup(identity)
                    except Exception:  # noqa: BLE001
                        gen = None
                    if gen is not None:
                        m.update({"state": "approved", "gen": str(gen)})
                # Settle EVERY round, changed or not: a crash between the
                # sync approval record and the finish-hook settlement leaves
                # a fully-decided round with no episode — recover it too.
                created, n = _settle_locked(data, plugin)
                created_any = created_any or created
                notes.extend(n)
            _save(data)
    except Exception:  # noqa: BLE001
        logger.exception("setup-round boot recovery failed")
        return
    for n in notes:
        await _note(n)
    if created_any and _kick is not None:
        _kick.set()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def start_worker() -> None:
    """Boot seam: start the supervised dispatch worker; it runs the boot
    recovery sweep first, then dispatches ``pending`` episodes."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.get_running_loop().create_task(
        _worker(), name="plugin-setup-episodes")
    if _kick is not None:
        _kick.set()


async def _worker() -> None:
    await _boot_recover()
    while True:
        try:
            assert _kick is not None
            await _kick.wait()
            _kick.clear()
            for ep in episodes("pending"):
                try:
                    await _run_episode(ep)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — isolate per episode
                    logger.exception("setup episode %s failed unexpectedly",
                                     ep.get("id"))
                    _update_episode(ep.get("id") or "", status="failed",
                                    last_error="internal error (see log)")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the worker must survive anything
            logger.exception("plugin-setup worker pass failed")
            await _sleep(5.0)
            if _kick is not None:
                _kick.set()  # self re-kick: never strand pending episodes


def _update_episode(episode_id: str, **fields) -> None:
    data = _load()
    for e in data["episodes"]:
        if e.get("id") == episode_id:
            e.update(fields, updated_ts=_now())
            break
    _save(data)


async def _run_episode(ep: dict) -> None:
    plugin = ep["plugin"]
    entry = None
    if _resolve_registry_entry is not None:
        try:
            entry = _resolve_registry_entry(plugin)
        except Exception:  # noqa: BLE001
            logger.exception("episode %s: registry resolve failed", ep["id"])
    if not isinstance(entry, dict) or entry.get("artifact_id") != ep["artifact_id"]:
        # Removed, or updated to a NEW artifact (whose own consent round will
        # mint its own episode) — this one must never fire (TOCTOU guard).
        _update_episode(ep["id"], status="stale",
                        last_error="plugin removed or artifact superseded")
        await _note(f"Plugin {plugin}: a queued setup run was dropped — the "
                    "plugin was removed or updated since the consent. A new "
                    "consent round owns the current version.")
        return
    # impl r4 (Sol): dispatch only against a LIVE route — the durable
    # approval/settlement happened regardless of the reconcile outcome (a
    # transient reconcile failure must not strand the round), but the setup
    # tool must not point the external service at an unrouted endpoint. The
    # episode stays pending; every reconcile kicks the worker to re-check.
    if _routes_live is not None:
        try:
            live = bool(_routes_live(plugin))
        except Exception:  # noqa: BLE001
            logger.exception("episode %s: routes_live check failed", ep["id"])
            live = False
        if not live:
            _update_episode(ep["id"],
                            last_error="waiting for live trigger route")
            return
    role, instruction = _compose(ep, entry)
    if role is None:
        _update_episode(ep["id"], status="failed", last_error=instruction)
        await _note(f"Plugin {plugin}: automatic setup could not run "
                    f"({instruction}). Run its setup tool manually.")
        return
    ok = False
    attempts = int(ep.get("attempts") or 0)
    while attempts < _MAX_DISPATCH_ATTEMPTS and not ok:
        attempts += 1
        if _dispatch is not None:
            try:
                ok = await _dispatch(role, instruction, {
                    "synthetic": "plugin_setup",
                    "setup_episode": ep["id"],
                })
            except Exception:  # noqa: BLE001
                logger.exception("episode %s: dispatch raised", ep["id"])
                ok = False
        if not ok and attempts < _MAX_DISPATCH_ATTEMPTS:
            await _sleep(_RETRY_BACKOFF_S[min(attempts - 1,
                                              len(_RETRY_BACKOFF_S) - 1)])
    if ok:
        # Bus accepted — the agent's own reply reports the setup OUTCOME to
        # the operator (disclosed: delivery, not result correlation).
        _update_episode(ep["id"], status="dispatched", attempts=attempts)
    else:
        _update_episode(ep["id"], status="failed", attempts=attempts,
                        last_error="dispatch not accepted")
        await _note(f"Plugin {plugin}: automatic setup dispatch failed — "
                    f"ask the agent to run its setup tool "
                    f"({ep['setup_tool']}) manually.")


async def _note(text: str) -> None:
    if _notify_operator is None:
        return
    try:
        await _notify_operator(text)
    except Exception:  # noqa: BLE001
        logger.exception("setup-episode operator note failed")


def _compose(ep: dict, entry: dict) -> tuple[str | None, str]:
    """Deterministic execution-target selection + the fixed Casa-authored
    instruction. Returns ``(role, instruction)`` or ``(None, reason)``.

    Tool binding is UNAMBIGUOUS or nothing: exactly one server-level grant
    is required — zero or several fail the episode (verify blocks such
    plugins upstream with ``setup_tool_ambiguous_server``).

    Target order: ``resident:assistant`` when targeted; else the
    lexicographically first resident; else the first specialist via
    assistant delegation (the specialist has no channel — the instruction
    names the EXACT specialist and tool and forbids substitution).
    Executor-only/empty targets are refused upstream at verify.
    """
    grants = sorted(entry.get("granted_tools") or [])
    if len(grants) != 1:
        return None, (f"ambiguous or missing MCP server binding "
                      f"({len(grants)} server grants)")
    tool = ep["setup_tool"]
    namespaced = f"{grants[0]}__{tool}"
    targets = entry.get("targets") or []
    residents = sorted(t.split(":", 1)[1] for t in targets
                       if t.startswith("resident:"))
    specialists = sorted(t.split(":", 1)[1] for t in targets
                         if t.startswith("specialist:"))
    base = (
        f"[casa plugin setup · episode {ep['id']}] The operator approved the "
        f"webhook trigger consent for plugin '{ep['plugin']}' and its secret "
        "was (re)minted. "
    )
    tail = (
        " Call it with no arguments, take no other action, and report the "
        "outcome briefly."
    )
    if "assistant" in residents:
        return "assistant", (
            base + f"Run the setup tool `{namespaced}` now to re-point the "
            "external service." + tail)
    if residents:
        return residents[0], (
            base + f"Run the setup tool `{namespaced}` now to re-point the "
            "external service." + tail)
    if specialists:
        sp = specialists[0]
        return "assistant", (
            base + f"Delegate to the specialist '{sp}' with the instruction "
            f"to run its setup tool `{namespaced}` now — do not substitute "
            "another agent or tool." + tail)
    return None, "no resident or specialist target"
