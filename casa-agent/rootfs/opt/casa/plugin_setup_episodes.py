"""Durable post-consent setup episodes (v0.112.0, casa-plugin-elevenlabs#2).

A plugin that declares ``casa.setupTool`` gets its setup tool run
AUTOMATICALLY once its trigger-consent episode settles with at least one
approval — the operator's Approve tap is the authorization for the wiring
that makes the trigger functional. Because plugin MCP tools surface only on
the plugin's target agents, Casa dispatches a synthetic Casa-authored turn
to the execution agent rather than calling the tool itself.

Design (Sol+Terra design round, 2026-07-24):

* **Durable, at-least-once**: an episode is persisted to
  ``/data/plugin-setup-episodes.json`` BEFORE dispatch and reconciled at
  boot — a restart between consent and dispatch cannot lose the run. The
  setup tool is argument-free + idempotent by authoring contract, so
  crash-induced duplicate execution is safe.
* **Settlement over ALL terminal decisions**: every Approve/Deny/expiry
  feeds :func:`on_consent_decision`; the episode settles when the plugin
  has no remaining pending consents. Approved subset non-empty → dispatch;
  deny-only → a DM note, never a dispatch (the "last decision is Deny"
  suppression hole from the design round is closed by evaluating on Deny
  too).
* **Atomic claim**: episode creation under the module lock, keyed by
  (plugin, artifact_id, approved-identity digest) — two racing settlement
  evaluations create ONE episode.
* **Exact-artifact binding (TOCTOU)**: the episode records the CONSENTED
  ``artifact_id``; the worker re-resolves the registry at dispatch time and
  marks the episode ``stale`` (with an operator note) when the plugin was
  removed or moved to a different artifact — a delayed episode from version
  N never fires against version N+1.
* **No plugin prose**: the synthetic turn is a fixed Casa-authored template;
  the only plugin-derived interpolations are grammar-validated identifiers
  (plugin name, namespaced tool name). The ``synthetic`` context marker is
  a RESERVED provenance key external ingress cannot spoof
  (``provenance.RESERVED_CONTEXT_KEYS``).
* **Delivery is not success**: ``dispatched`` means the bus accepted the
  turn; the execution agent's own reply reports the actual setup outcome to
  the operator. Episodes stuck ``pending``/``failed``/``stale`` surface as
  plugin-health issues (documented weaker-than-result-correlated semantics).
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
_SCHEMA_VERSION = 1
_MAX_DISPATCH_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 5.0)

# Wired by casa_core at boot. All optional — absent seams degrade to logging.
_dispatch: Callable[[str, str, dict], Awaitable[bool]] | None = None
_notify_operator: Callable[[str], Awaitable[None]] | None = None
_pending_consents_for: Callable[[str], int] | None = None
_resolve_registry_entry: Callable[[str], Any] | None = None
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

_lock: asyncio.Lock | None = None
_worker_task: asyncio.Task | None = None
_kick: asyncio.Event | None = None
# Per-plugin decision accumulator for the OPEN (unsettled) consent episode:
# {plugin: {"artifact_id": str, "approved": [identity...], "denied": int}}
_open_decisions: dict[str, dict] = {}


def _now() -> float:
    return time.time()


def configure(*, dispatch, notify_operator, pending_consents_for,
              resolve_registry_entry, sleep=asyncio.sleep) -> None:
    """casa_core boot wiring. Idempotent."""
    global _dispatch, _notify_operator, _pending_consents_for
    global _resolve_registry_entry, _sleep, _lock, _kick
    _dispatch = dispatch
    _notify_operator = notify_operator
    _pending_consents_for = pending_consents_for
    _resolve_registry_entry = resolve_registry_entry
    _sleep = sleep
    if _lock is None:
        _lock = asyncio.Lock()
    if _kick is None:
        _kick = asyncio.Event()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _load() -> dict:
    if not STORE_PATH.is_file():
        return {"schema_version": _SCHEMA_VERSION, "episodes": []}
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(
                data.get("episodes"), list):
            raise ValueError("malformed store")
        return data
    except Exception:  # noqa: BLE001 — a corrupt store must not brick boot
        logger.exception("plugin-setup-episodes store unreadable — resetting")
        return {"schema_version": _SCHEMA_VERSION, "episodes": []}


def _save(data: dict) -> None:
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, STORE_PATH)


def episodes(status: str | None = None) -> list[dict]:
    eps = _load()["episodes"]
    return [e for e in eps if status is None or e.get("status") == status]


def health_issues() -> list[dict]:
    """Non-terminal-success episodes for plugin-health regeneration."""
    out = []
    for e in episodes():
        if e.get("status") in ("pending", "failed", "stale"):
            out.append({
                "kind": f"setup_episode_{e['status']}",
                "plugin": e.get("plugin"),
                "episode": e.get("id"),
                "detail": e.get("last_error") or "",
            })
    return out


def _episode_key(plugin: str, artifact_id: str,
                 approved: list[str]) -> str:
    h = hashlib.sha256()
    h.update(plugin.encode())
    h.update(artifact_id.encode())
    for ident in sorted(approved):
        h.update(ident.encode())
    return h.hexdigest()[:24]


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

async def on_consent_decision(*, plugin: str, artifact_id: str,
                              identity: str, approved: bool) -> None:
    """Feed ONE terminal consent decision (Approve / Deny / expiry counts as
    deny). Called by the consent finish hook AFTER the decision is durable
    (ack persisted + reconcile for approvals). Never raises."""
    if _lock is None:
        return  # not configured (tests construct explicitly)
    try:
        async with _lock:
            acc = _open_decisions.setdefault(
                plugin, {"artifact_id": artifact_id, "approved": [],
                         "denied": 0})
            # A new artifact generation supersedes the open accumulator.
            if acc.get("artifact_id") != artifact_id:
                acc.update({"artifact_id": artifact_id, "approved": [],
                            "denied": 0})
            if approved:
                if identity not in acc["approved"]:
                    acc["approved"].append(identity)
            else:
                acc["denied"] += 1
        await _maybe_settle(plugin)
    except Exception:  # noqa: BLE001 — consent flow must never see a raise
        logger.exception("setup-episode decision handling failed (plugin=%s)",
                         plugin)


async def _maybe_settle(plugin: str) -> None:
    assert _lock is not None
    pending = 0
    if _pending_consents_for is not None:
        try:
            pending = _pending_consents_for(plugin)
        except Exception:  # noqa: BLE001
            logger.exception("pending-consent count failed (plugin=%s)", plugin)
            return  # cannot prove settlement — a later decision retries
    if pending > 0:
        return
    async with _lock:
        acc = _open_decisions.pop(plugin, None)
        if acc is None:
            return
        approved = acc["approved"]
        artifact_id = acc["artifact_id"]
        if not approved:
            logger.info("consent episode settled with no approvals "
                        "(plugin=%s) — setup not run", plugin)
            if _notify_operator is not None:
                await _notify_operator(
                    f"Plugin {plugin}: consent settled with no approved "
                    "triggers — its setup tool was not run.")
            return
        entry = None
        if _resolve_registry_entry is not None:
            try:
                entry = _resolve_registry_entry(plugin)
            except Exception:  # noqa: BLE001
                logger.exception("registry resolve failed (plugin=%s)", plugin)
        setup = (entry or {}).get("setup_tool") if isinstance(entry, dict) else None
        if not setup:
            return  # plugin declares no setup tool — nothing to do
        key = _episode_key(plugin, artifact_id, approved)
        data = _load()
        if any(e.get("key") == key for e in data["episodes"]):
            return  # atomic claim: already created by a racing settlement
        data["episodes"].append({
            "id": uuid.uuid4().hex[:12],
            "key": key,
            "plugin": plugin,
            "artifact_id": artifact_id,
            "setup_tool": setup,
            "approved_identities": sorted(approved),
            "status": "pending",
            "attempts": 0,
            "created_ts": _now(),
            "updated_ts": _now(),
        })
        _save(data)
    if _kick is not None:
        _kick.set()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def start_worker() -> None:
    """Boot seam: start (or restart) the supervised dispatch worker and kick
    it once so boot-reconciled ``pending`` episodes re-dispatch."""
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
                await _run_episode(ep)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the worker must survive anything
            logger.exception("plugin-setup worker pass failed")
            await _sleep(5.0)


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

    Order (design round): ``resident:assistant`` when targeted; else the
    lexicographically first resident target; else the first specialist
    target via assistant delegation (the specialist has no channel — the
    instruction names the EXACT specialist and tool and forbids
    substitution). Executor-only targets are refused upstream at verify;
    reaching here anyway reports unsupported.
    """
    targets = entry.get("targets") or []
    residents = sorted(t.split(":", 1)[1] for t in targets
                       if t.startswith("resident:"))
    specialists = sorted(t.split(":", 1)[1] for t in targets
                         if t.startswith("specialist:"))
    grants = entry.get("granted_tools") or []
    tool = ep["setup_tool"]
    namespaced = next(
        (f"{g}__{tool}" for g in sorted(grants)), tool)
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
