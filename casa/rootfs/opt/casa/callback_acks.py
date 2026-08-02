"""Persistent operator-consent acks for plugin-declared authorization
callbacks.

One ack = the operator approved exactly one consent IDENTITY
(:func:`plugin_callbacks.ack_identity` — plugin + effective callback name +
declaration digest). Unlike trigger acks, a callback grants no turn into a
role and no memory access — the identity deliberately excludes the artifact
id, so a routine plugin upgrade that leaves the declaration unchanged keeps
its ack; only an operator-visible change (rename, new fields later) yields a
new identity and forces re-consent.

Properties (structural sibling of ``trigger_acks.py``):

* **Atomic** — every mutation persists via ``atomic_io.atomic_write_text``
  (sidecar + fsync + ``os.replace``); a crash mid-write can never leave a
  half-written store that later parses into unintended consent.
* **Fail-closed** — a missing, unreadable, or malformed store means NO acks
  (callbacks stay unrouted); it never raises into the reconciler. The next
  successful ``record`` rewrites a valid store. (INV-CB-003.)
* **Thread-safe** — a ``threading.Lock`` guards state: ``record`` runs on
  the event loop (Telegram approve callback), revocation runs from the
  plugin lifecycle path, and pruning runs from the reconciler.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ACKS_PATH = Path("/data/callback_acks.json")

_SCHEMA_VERSION = 1

#: The EXACT key set a stored ack record may carry. A record with any key
#: outside this set is malformed and fails the whole store (INV-CB-003): a
#: hand-edited / merged file that smuggled an extra field is no more
#: trustworthy than one with a bad identity.
_ACK_RECORD_KEYS = frozenset(
    {"plugin", "effective", "declaration_digest", "gen", "ts"})


class CallbackAckStore:
    def __init__(self, path: Path = ACKS_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._acks: dict[str, dict[str, Any]] = self._load()

    # -- load / persist ------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        """Whole-store fail-closed load (mirrors trigger_acks.py): wrong
        schema, any malformed record, or any key that does not equal the
        RECOMPUTED identity of its own record ⇒ NO acks at all. A truncated
        / merged / hand-edited store can never manufacture consent; the
        operator simply re-consents. (INV-CB-003.)"""
        from plugin_callbacks import ack_identity

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            return {}
        acks = raw.get("acks")
        if not isinstance(acks, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for ident, rec in acks.items():
            if not (isinstance(ident, str) and isinstance(rec, dict)):
                return {}
            # Exact schema: any key outside the record's declared set makes the
            # WHOLE store untrusted (an extra field means the file was written
            # by something other than this store).
            if set(rec) != _ACK_RECORD_KEYS:
                return {}
            fields = {k: rec.get(k)
                      for k in ("plugin", "effective", "declaration_digest")}
            gen = rec.get("gen")
            ts = rec.get("ts")
            if (not all(isinstance(v, str) and v for v in fields.values())
                    or not (isinstance(gen, str) and gen)
                    # ``ts`` must be a real finite number — an int or float,
                    # never a bool (a subclass of int) and never NaN/inf.
                    or isinstance(ts, bool)
                    or not isinstance(ts, (int, float))
                    or not math.isfinite(ts)):
                return {}
            try:
                expected = ack_identity(**fields)
            except Exception:  # noqa: BLE001 — unhashable garbage ⇒ closed
                return {}
            if expected != ident:
                return {}
            out[ident] = rec
        return out

    def _persist_candidate_locked(self, candidate: dict[str, dict[str, Any]]) -> None:
        """Durably write *candidate*; the caller publishes it to ``_acks``
        ONLY afterwards — a failed write raises with the in-memory view
        unchanged, so a racing reconcile can never route (or drop) state
        that would revert on reboot."""
        from atomic_io import atomic_write_text
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps({"schema_version": _SCHEMA_VERSION, "acks": candidate},
                       indent=2, sort_keys=True) + "\n",
        )

    # -- queries -------------------------------------------------------------

    def get(self, identity: str) -> "dict[str, Any] | None":
        """The full ack record for *identity* (a copy), or ``None``."""
        with self._lock:
            rec = self._acks.get(identity)
            return dict(rec) if rec is not None else None

    # -- mutations (each persists atomically before returning) ---------------

    def record(self, plugin: str, effective: str, declaration_digest: str) -> dict[str, Any]:
        """Record the operator's consent for this (plugin, effective,
        declaration_digest) identity (idempotent); returns the stored
        record (a copy)."""
        from plugin_callbacks import ack_identity
        identity = ack_identity(plugin, effective, declaration_digest)
        rec = {
            "plugin": plugin,
            "effective": effective,
            "declaration_digest": declaration_digest,
            "ts": int(time.time()),
        }
        with self._lock:
            # Per-approval GENERATION (mirrors trigger_acks): unique per
            # approval so re-approval after a revoke (record gone)
            # necessarily yields a new (identity, gen) pair. Re-recording a
            # LIVE ack (idempotent duplicate) keeps the generation.
            existing = self._acks.get(identity)
            rec["gen"] = (existing["gen"] if existing and existing.get("gen")
                          else uuid.uuid4().hex)
            candidate = dict(self._acks)
            candidate[identity] = rec
            self._persist_candidate_locked(candidate)
            self._acks = candidate
            return dict(rec)

    def revoke_plugin(self, plugin: str) -> list[dict[str, Any]]:
        """Drop every ack recorded for *plugin*; returns the removed
        records."""
        return self._revoke(lambda rec: rec.get("plugin") == plugin)

    def revoke_effective(self, plugin: str, effective: str) -> list[dict[str, Any]]:
        """Drop every ack for one (plugin, effective) callback — across any
        declaration digest. The single-callback operator off-switch backing
        the ``callback_ack_revoke`` tool. Returns the removed
        records."""
        return self._revoke(lambda rec: rec.get("plugin") == plugin
                            and rec.get("effective") == effective)

    def prune_stale(self, valid_identities: set[str]) -> list[dict[str, Any]]:
        """Opportunistic reconcile prune: drop every ack whose
        identity is not in *valid_identities* (no installed declaration can
        still compute it); returns the removed records."""
        with self._lock:
            matched = [i for i in self._acks if i not in valid_identities]
            if not matched:
                return []
            candidate = dict(self._acks)
            removed = [candidate.pop(i) for i in matched]
            self._persist_candidate_locked(candidate)
            self._acks = candidate
            return removed

    def _revoke(self, predicate) -> list[dict[str, Any]]:
        with self._lock:
            matched = [i for i, rec in self._acks.items() if predicate(rec)]
            if not matched:
                return []
            candidate = dict(self._acks)
            removed = [candidate.pop(i) for i in matched]
            self._persist_candidate_locked(candidate)
            self._acks = candidate
            return removed


# Process-wide singleton (mirrors ACKS in trigger_acks): the reconciler, the
# callback consent approve callback, and the plugin lifecycle revocation
# path all share it.
ACKS = CallbackAckStore()
