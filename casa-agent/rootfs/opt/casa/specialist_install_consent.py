"""Structural operator-consent gate for specialist/persona installs — the
SAME shape as trigger_consent.py's plugin-trigger consent: a DM
Approve/Deny keyboard whose Approve synchronously persists a fail-closed,
atomically-written ack BEFORE any CAS write or activation. Never a checksum
string an LLM tool call could echo back itself."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from canonical_bytes import checksum_json

logger = logging.getLogger(__name__)

INSTALL_CONSENT_TTL_S = 600.0
_ACKS_PATH = Path("/data/specialist_install_acks.json")
_SCHEMA_VERSION = 1


def install_consent_identity(*, component_id: str, version: str, component_checksum: str,
                              slug: str) -> str:
    """The ONE identity-derivation function for a specialist-install consent
    decision (mirrors plugin_triggers.ack_identity). Binds the approval to
    the EXACT inspected component — a re-fetch that changes any of these
    four fields yields a different identity, so a stale approval can never
    ack a different artifact.

    Round-2 (finding #2): the parameter is still named `component_checksum`
    for call-site compatibility, but EVERY production caller now passes
    `inspection.root_digest` (compute_install_root_digest's full-closure
    digest — role+doctrine+config-schema+manifest+persona+dependencies), not
    `inspection.component_checksum` (the narrow 3-file digest, which is
    still a separate field on InspectionResult for CAS-neutral display/
    logging only). Binding consent to the narrow digest let a persona/
    corpus/plugin substitution slip past an operator's approval unnoticed."""
    return checksum_json({
        "component_id": component_id, "version": version,
        "component_checksum": component_checksum, "slug": slug,
    })


@dataclass(frozen=True)
class SpecialistInstallConsentKey:
    component_id: str
    slug: str
    identity: str


class SpecialistInstallAckStore:
    """Fail-closed, atomically-persisted install-consent acks — structurally
    identical to trigger_acks.TriggerAckStore (whole-store fail-closed load,
    identity recomputed and compared on load, atomic_write_text on every
    mutation)."""

    def __init__(self, path: Path = _ACKS_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._acks: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
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
            fields = {k: rec.get(k) for k in ("component_id", "version",
                                                "component_checksum", "slug")}
            if not all(isinstance(v, str) and v for v in fields.values()):
                return {}
            if install_consent_identity(**fields) != ident:
                return {}
            out[ident] = rec
        return out

    def _persist_locked(self, candidate: dict[str, dict[str, Any]]) -> None:
        from atomic_io import atomic_write_text
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps({"schema_version": _SCHEMA_VERSION, "acks": candidate},
                       indent=2, sort_keys=True) + "\n",
        )

    def is_acked(self, identity: str) -> bool:
        with self._lock:
            return identity in self._acks

    def get(self, identity: str) -> "dict[str, Any] | None":
        with self._lock:
            rec = self._acks.get(identity)
            return dict(rec) if rec is not None else None

    def record(self, *, identity: str, component_id: str, version: str,
               component_checksum: str, slug: str) -> None:
        rec = {"component_id": component_id, "version": version,
               "component_checksum": component_checksum, "slug": slug,
               "ts": int(time.time())}
        with self._lock:
            candidate = dict(self._acks)
            candidate[identity] = rec
            self._persist_locked(candidate)
            self._acks = candidate

    def revoke(self, identity: str) -> bool:
        with self._lock:
            if identity not in self._acks:
                return False
            candidate = dict(self._acks)
            del candidate[identity]
            self._persist_locked(candidate)
            self._acks = candidate
            return True


def render_install_consent_message(inspection: Any) -> str:
    # Round-3 fix (finding #7): the identity this consent binds
    # (`install_consent_identity(..., component_checksum=inspection.
    # root_digest, ...)` below) is keyed on `root_digest` — the FULL-CLOSURE
    # digest of the component PLUS every resolved dependency — not the
    # narrow `component_checksum`. Show `root_digest` (what the tap actually
    # binds) plus each resolved dependency's own digest, so the operator can
    # see exactly what the approval covers; `component_checksum` stays for
    # reference.
    deps = ", ".join(f"{d.kind}:{d.identifier}" for d in inspection.dependencies)
    dep_digest_lines = "".join(
        f"  - {d.kind}:{d.identifier} = {d.digest}\n" for d in inspection.dependencies)
    return (
        "\U0001F510 Specialist install consent\n\n"
        f"Install '{inspection.component_id}@{inspection.version}' as "
        f"specialist:{inspection.slug}?\n"
        f"Mission: {inspection.mission}\n"
        f"Default persona: {inspection.default_persona_ref}\n"
        f"Dependencies: {deps or '(none)'}\n"
        f"{dep_digest_lines}"
        f"Root digest (approved — component + dependencies): {inspection.root_digest}\n"
        f"Component checksum: {inspection.component_checksum}\n\n"
        "Approve to install; Deny to discard the staged fetch."
    )


def prompt_specialist_install_consent(
    *, coordinator: Any, channel: Any, chat_id: int, operator_id: int, inspection: Any,
    acks: "SpecialistInstallAckStore",
    reconcile_cb: "Callable[[], Awaitable[None]] | None" = None,
) -> Any:
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        component_checksum=inspection.root_digest, slug=inspection.slug,
    )
    key = SpecialistInstallConsentKey(
        component_id=inspection.component_id, slug=inspection.slug, identity=identity)
    text = render_install_consent_message(inspection)

    def _on_commit_sync(idx: int, meta: dict) -> None:
        if idx == 0:
            acks.record(identity=identity, component_id=inspection.component_id,
                        version=inspection.version,
                        component_checksum=inspection.root_digest, slug=inspection.slug)
            meta["acked"] = True

    def _finish_factory(message_id: int, req: Any) -> Callable[[dict], Any]:
        async def _finish(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"⌛ Expired — install consent for {inspection.slug!r} was not "
                    "answered; nothing was installed",
                )
                return
            if outcome.get("option_index") == 0:
                if not req.meta.get("acked"):
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        "internal error recording install consent — re-run the install to "
                        "be prompted again",
                    )
                    return
                await channel.edit_dm_message(
                    chat_id, message_id, f"✅ Approved — installing {inspection.slug!r}",
                )
                if reconcile_cb is not None:
                    try:
                        await reconcile_cb()
                    except Exception:  # noqa: BLE001 — surface, never raise
                        logger.exception(
                            "post-consent specialist install commit failed (slug=%s)",
                            inspection.slug)
                        await channel.edit_dm_message(
                            chat_id, message_id,
                            f"⚠️ Approved, but installing {inspection.slug!r} "
                            "failed — check the configurator topic",
                        )
            else:
                await channel.edit_dm_message(
                    chat_id, message_id, f"❌ Denied — {inspection.slug!r} was not installed",
                )

        return _finish

    return coordinator.register_challenge(
        key, chat_id=chat_id, operator_id=operator_id, channel=channel,
        challenge_text=text, options=["Approve", "Deny"],
        on_commit_sync=_on_commit_sync, finish_factory=_finish_factory,
        kind="specialist_install_consent",
        meta_extra={"install_slug": inspection.slug, "install_component_id": inspection.component_id},
        timeout_s=INSTALL_CONSENT_TTL_S,
    )
