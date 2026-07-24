"""Durable post-consent setup episodes (v0.112.0, casa-plugin-elevenlabs#2).

A plugin that declares ``casa.setupTool`` gets its setup tool run
AUTOMATICALLY once its trigger-consent round settles with at least one
approval — the operator's Approve tap is the authorization for the wiring
that makes the trigger functional. Because plugin MCP tools surface only on
the plugin's target agents, Casa dispatches a synthetic Casa-authored turn
to the execution agent rather than calling the tool itself.

Design (Sol+Terra design round + implementation round 1, 2026-07-24):

* **Round ledger, not ack counting** (impl r1 P1): the consent ROUND is
  tracked by this module — every prompted consent registers its identity as
  an OPEN member (:func:`on_consent_prompted`, fed from the prompt path);
  every terminal decision (Approve / Deny / expiry) marks its member
  (:func:`on_consent_decision`). Settlement = ALL members decided. Denials
  and expiries settle the round exactly like approvals — the reconciler's
  ``trigger_pending_ack`` state (which a denial never clears) is not
  consulted. A re-prompt after expiry RE-OPENS the member, so the round
  waits again.
* **Durable across the whole consent-to-dispatch interval** (impl r1 P1):
  the round ledger AND the episodes live in
  ``/data/plugin-setup-episodes.json`` (atomic tmp+rename), updated on
  every prompt/decision — a restart mid-round loses nothing.
* **Single-lock settlement** (impl r1 P1): accumulate + settle-check +
  episode creation happen under ONE lock acquisition with no awaits
  in between — concurrent finish callbacks cannot split a round into two
  episodes. Operator notes and the worker kick happen after release.
* **Re-consent mints a new episode** (impl r1 P1): approve decisions carry
  ``identity#gen`` (the persisted ack's approval generation) — a
  revoke→re-approve or update-rotation with an identical trigger tuple
  produces a NEW generation, hence a new episode key.
* **Exact-artifact binding (TOCTOU)**: the episode records the CONSENTED
  ``artifact_id``; the worker re-resolves at dispatch time and marks the
  episode ``stale`` (operator note) when the plugin was removed or
  superseded.
* **Unambiguous tool binding** (impl r1 P1): the instruction names the
  EXACT namespaced tool ``<server-grant>__<setupTool>``; a plugin with
  zero or multiple server grants FAILS the episode with a clear reason
  (and verify blocks multi-server setup-tool plugins upstream) — never an
  unqualified or guessed name.
* **Worker survivability** (impl r1 P1): per-episode failures are isolated
  and the worker re-kicks itself after an unexpected pass failure, so one
  bad episode can never strand the rest until the next external kick.
* **Terminal-state hygiene** (impl r1 P2): a new episode for a plugin
  prunes that plugin's older episodes, and ``failed``/``stale`` rows decay
  out of health after :data:`_HEALTH_DECAY_S` — no permanent residue once
  the situation is resolved or superseded.
* **No plugin prose**: the synthetic turn is a fixed Casa-authored
  template; the only plugin-derived interpolations are grammar-validated
  identifiers. The ``synthetic`` context marker is a RESERVED provenance
  key external ingress cannot spoof.
* **Delivery is not success** (disclosed): ``dispatched`` means the bus
  accepted the turn; the execution agent's reply reports the actual
  outcome. ``pending``/``failed``/``stale`` surface as plugin-health
  issues.
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
_SCHEMA_VERSION = 2
_MAX_DISPATCH_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 5.0)
_HEALTH_DECAY_S = 72 * 3600.0

# Wired by casa_core at boot. All optional — absent seams degrade to logging.
_dispatch: Callable[[str, str, dict], Awaitable[bool]] | None = None
_notify_operator: Callable[[str], Awaitable[None]] | None = None
_resolve_registry_entry: Callable[[str], Any] | None = None
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

_lock: asyncio.Lock | None = None
_worker_task: asyncio.Task | None = None
_kick: asyncio.Event | None = None


def _now() -> float:
    return time.time()


def configure(*, dispatch, notify_operator, resolve_registry_entry,
              sleep=asyncio.sleep) -> None:
    """casa_core boot wiring. Idempotent."""
    global _dispatch, _notify_operator, _resolve_registry_entry
    global _sleep, _lock, _kick
    _dispatch = dispatch
    _notify_operator = notify_operator
    _resolve_registry_entry = resolve_registry_entry
    _sleep = sleep
    if _lock is None:
        _lock = asyncio.Lock()
    if _kick is None:
        _kick = asyncio.Event()


# ---------------------------------------------------------------------------
# Store: {"schema_version", "rounds": {plugin: {...}}, "episodes": [...]}
# ---------------------------------------------------------------------------

def _load() -> dict:
    if not STORE_PATH.is_file():
        return {"schema_version": _SCHEMA_VERSION, "rounds": {},
                "episodes": []}
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if (not isinstance(data, dict)
                or not isinstance(data.get("episodes"), list)):
            raise ValueError("malformed store")
        data.setdefault("rounds", {})
        return data
    except Exception:  # noqa: BLE001 — a corrupt store must not brick boot
        logger.exception("plugin-setup-episodes store unreadable — resetting")
        return {"schema_version": _SCHEMA_VERSION, "rounds": {},
                "episodes": []}


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
    ``pending`` never decays (it is actionable until dispatched)."""
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

def register_prompt(*, plugin: str, artifact_id: str, identity: str) -> None:
    """Register (or RE-OPEN) a round member when its consent keyboard is
    posted. Fed SYNCHRONOUSLY from the consent prompt path (which runs
    without awaits inside the event loop, so this read-modify-write cannot
    interleave with the locked async sections — both are yield-free). The
    round's membership comes from what was actually ASKED, never inferred
    from ack state; a re-prompt after expiry RE-OPENS the member so the
    round waits for the fresh decision. Idempotent; never raises."""
    try:
        data = _load()
        rnd = data["rounds"].get(plugin)
        if not isinstance(rnd, dict) or rnd.get("artifact_id") != artifact_id:
            rnd = {"artifact_id": artifact_id, "members": {}}
            data["rounds"][plugin] = rnd
        rnd["members"][identity] = {"state": "open"}
        _save(data)
    except Exception:  # noqa: BLE001 — prompt path must never see a raise
        logger.exception("setup-round prompt registration failed (plugin=%s)",
                         plugin)


async def on_consent_decision(*, plugin: str, artifact_id: str,
                              identity: str, approved: bool,
                              approval_gen: str = "") -> None:
    """Feed ONE terminal consent decision (Approve / Deny / expiry counts as
    deny). Called AFTER the decision is durable (ack persisted + reconcile
    succeeded, for approvals). ``approval_gen`` is the persisted ack's
    approval generation — it keys the episode so a re-approval (new gen)
    mints a NEW episode even for an identical trigger tuple. Never raises.

    Accumulate + settle-check + episode creation run under ONE lock
    acquisition (no awaits inside) — concurrent finish callbacks cannot
    split a round. Notes and the worker kick happen after release."""
    if _lock is None:
        return
    note: str | None = None
    created = False
    try:
        async with _lock:
            data = _load()
            rnd = data["rounds"].get(plugin)
            if isinstance(rnd, dict) and rnd.get("artifact_id") != artifact_id:
                # impl r2 (both reviewers): a LATE decision from a superseded
                # artifact must never replace the CURRENT round — the prompt
                # path (which only ever runs from a live reconcile) is the
                # sole authority for starting a new-artifact round. Ignore
                # the stale decision outright.
                logger.info(
                    "stale consent decision ignored (plugin=%s artifact=%s, "
                    "current round is %s)", plugin, artifact_id,
                    rnd.get("artifact_id"))
                return
            if not isinstance(rnd, dict):
                # Unknown round (e.g. store reset) — synthesize one so a
                # decision is never dropped on the floor.
                rnd = {"artifact_id": artifact_id, "members": {}}
                data["rounds"][plugin] = rnd
            member = {"state": "approved" if approved else "denied"}
            if approved:
                member["gen"] = approval_gen
            rnd["members"][identity] = member
            undecided = [i for i, m in rnd["members"].items()
                         if m.get("state") == "open"]
            if undecided:
                _save(data)
                return
            # Round settled — consume it.
            del data["rounds"][plugin]
            approved_keys = sorted(
                f"{i}#{m.get('gen', '')}"
                for i, m in rnd["members"].items()
                if m.get("state") == "approved")
            if not approved_keys:
                _save(data)
                note = (f"Plugin {plugin}: consent settled with no approved "
                        "triggers — its setup tool was not run.")
            else:
                entry = None
                if _resolve_registry_entry is not None:
                    try:
                        entry = _resolve_registry_entry(plugin)
                    except Exception:  # noqa: BLE001
                        logger.exception("registry resolve failed (plugin=%s)",
                                         plugin)
                setup = (entry or {}).get("setup_tool") \
                    if isinstance(entry, dict) else None
                if not setup:
                    _save(data)  # no setup tool — nothing to do
                else:
                    key = _episode_key(plugin, artifact_id, approved_keys)
                    consumed = data.setdefault("consumed_keys", [])
                    if (not any(e.get("key") == key
                                for e in data["episodes"])
                            and key not in consumed):
                        # Terminal-state hygiene: a fresh episode supersedes
                        # the plugin's older ones (any status) — but their
                        # KEYS are kept as bounded tombstones so a replayed
                        # stale decision can never recreate a consumed
                        # episode and prune the current one (impl r2, Sol).
                        for old in data["episodes"]:
                            if (old.get("plugin") == plugin
                                    and old.get("key")):
                                consumed.append(old["key"])
                        del consumed[:-50]
                        data["episodes"] = [
                            e for e in data["episodes"]
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
                        created = True
                    _save(data)
    except Exception:  # noqa: BLE001 — consent flow must never see a raise
        logger.exception("setup-episode decision handling failed (plugin=%s)",
                         plugin)
        return
    if note is not None:
        await _note(note)
    if created and _kick is not None:
        _kick.set()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def start_worker() -> None:
    """Boot seam: start the supervised dispatch worker and kick it once so
    boot-reconciled ``pending`` episodes re-dispatch."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.get_running_loop().create_task(
        _worker(), name="plugin-setup-episodes")
    if _kick is not None:
        _kick.set()


async def _worker() -> None:
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
        # the operator (documented: delivery, not result correlation).
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

    Tool binding is UNAMBIGUOUS or nothing (impl r1 P1): exactly one
    server-level grant is required to construct the namespaced tool name —
    zero or several grants fail the episode (verify blocks multi-server
    setup-tool plugins upstream with ``setup_tool_ambiguous_server``).

    Target order (design round): ``resident:assistant`` when targeted; else
    the lexicographically first resident target; else the first specialist
    target via assistant delegation (the specialist has no channel — the
    instruction names the EXACT specialist and tool and forbids
    substitution). Executor-only targets are refused upstream at verify.
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
