"""Persistent operator-consent acks for plugin-declared webhook triggers
(Release B).

One ack = the operator approved exactly one consent IDENTITY
(:func:`plugin_triggers.ack_identity` — plugin + artifact + effective name +
target + normalized auth policy). Records are STRUCTURED (not bare hashes) so
lifecycle revocation can answer "which plugin/artifact/effective names were
consented" — `revoke_artifact` returns the removed records and the caller
retires the matching per-trigger secrets.

Properties:

* **Atomic** — every mutation persists via ``atomic_io.atomic_write_text``
  (sidecar + fsync + ``os.replace``); a crash mid-write can never leave a
  half-written store that later parses into unintended consent.
* **Fail-closed** — a missing, unreadable, or malformed store means NO acks
  (triggers stay unrouted, ``trigger_pending_ack``); it never raises into the
  reconciler. The next successful ``record`` rewrites a valid store.
* **Thread-safe** — a ``threading.Lock`` guards state: ``record`` runs on the
  event loop (Telegram approve callback), revocation runs from the plugin
  lifecycle path, and health regeneration reads from a worker thread.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ACKS_PATH = Path("/data/webhook_trigger_acks.json")

_SCHEMA_VERSION = 1


class TriggerAckStore:
    def __init__(self, path: Path = ACKS_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._acks: dict[str, dict[str, Any]] = self._load()

    # -- load / persist ------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        acks = raw.get("acks") if isinstance(raw, dict) else None
        if not isinstance(acks, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for ident, rec in acks.items():
            if isinstance(ident, str) and isinstance(rec, dict):
                out[ident] = rec
        return out

    def _persist_locked(self) -> None:
        from atomic_io import atomic_write_text
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps({"schema_version": _SCHEMA_VERSION, "acks": self._acks},
                       indent=2, sort_keys=True) + "\n",
        )

    # -- queries -------------------------------------------------------------

    def is_acked(self, identity: str) -> bool:
        with self._lock:
            return identity in self._acks

    # -- mutations (each persists atomically before returning) ---------------

    def record(
        self, *, identity: str, plugin: str, artifact_id: str,
        effective: str, target: str, auth: dict[str, Any],
    ) -> None:
        """Record the operator's consent for *identity* (idempotent)."""
        rec = {
            "plugin": plugin,
            "artifact_id": artifact_id,
            "effective": effective,
            "target": target,
            "auth": dict(auth),
            "ts": int(time.time()),
        }
        with self._lock:
            self._acks[identity] = rec
            self._persist_locked()

    def revoke_plugin(self, plugin: str) -> list[dict[str, Any]]:
        """Drop every ack recorded for *plugin*; returns the removed records."""
        return self._revoke(lambda rec: rec.get("plugin") == plugin)

    def revoke_artifact(self, artifact_id: str) -> list[dict[str, Any]]:
        """Drop every ack bound to *artifact_id*; returns the removed records
        (the caller retires the matching per-trigger secrets)."""
        return self._revoke(lambda rec: rec.get("artifact_id") == artifact_id)

    def _revoke(self, predicate) -> list[dict[str, Any]]:
        with self._lock:
            matched = [i for i, rec in self._acks.items() if predicate(rec)]
            removed = [self._acks.pop(i) for i in matched]
            if removed:
                self._persist_locked()
            return removed


# Process-wide singleton (mirrors GRANTS/CHALLENGES): the reconciler, the
# consent approve callback, and the lifecycle revocation path all share it.
ACKS = TriggerAckStore()
