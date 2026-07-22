"""Bare-persona repo install/apply (spec §2.1, §9.4 decision 4) — generalizes
Task N1a's fetch/validate/consent pipeline for a MUCH smaller artifact (no
role, no dependency closure, no config schema) and applies the result as an
override binding, reusing Plan 1 Task 8's swap machinery exactly."""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from canonical_bytes import checksum_json
from specialist_install import SpecialistInstallError, resolve_and_fetch

if TYPE_CHECKING:
    from persona_pack import PersonaPack
    from role_slot import RoleSlot

__all__ = [
    "PersonaInspectionResult",
    "inspect_persona_repo",
    "persona_install_consent_identity",
    "PersonaInstallAckStore",
    "commit_persona_install",
    "apply_persona_override",
]


@dataclass(frozen=True, slots=True)
class PersonaInspectionResult:
    persona_id: str
    version: str
    checksum: str
    display_name: str
    staged_dir: Path


def inspect_persona_repo(
    repo: str, ref: str, *, subdir: str = "", expected_revision: str | None = None,
    staging_root: Path = Path("/config/personas/.staging"),
) -> PersonaInspectionResult:
    from persona_pack import PersonaPackError, load_persona_pack

    staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = staging_root / uuid.uuid4().hex
    resolve_and_fetch(repo, ref, subdir, dest, expected_revision=expected_revision)
    manifest_path = dest / "manifest.json"
    if not manifest_path.is_file():
        raise SpecialistInstallError("manifest_missing", f"{repo}@{ref}: manifest.json not found")
    try:
        pack = load_persona_pack(dest / "pack", manifest_path)
    except PersonaPackError as exc:
        raise SpecialistInstallError("persona_invalid", str(exc)) from exc
    return PersonaInspectionResult(
        persona_id=pack.persona_id, version=pack.version, checksum=pack.checksum,
        display_name=pack.identity.get("display_name", pack.persona_id), staged_dir=dest,
    )


_ACKS_PATH = Path("/data/persona_install_acks.json")
_SCHEMA_VERSION = 1


def persona_install_consent_identity(*, persona_id: str, version: str, checksum: str) -> str:
    return checksum_json({"persona_id": persona_id, "version": version, "checksum": checksum})


class PersonaInstallAckStore:
    """Same fail-closed/atomic-write shape as SpecialistInstallAckStore and
    trigger_acks.TriggerAckStore — a third sibling on the SAME structural
    pattern, not a fourth divergent design."""

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
            fields = {k: rec.get(k) for k in ("persona_id", "version", "checksum")}
            if not all(isinstance(v, str) and v for v in fields.values()):
                return {}
            if persona_install_consent_identity(**fields) != ident:
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

    def record(self, *, identity: str, persona_id: str, version: str, checksum: str) -> None:
        rec = {"persona_id": persona_id, "version": version, "checksum": checksum,
               "ts": int(time.time())}
        with self._lock:
            candidate = dict(self._acks)
            candidate[identity] = rec
            self._persist_locked(candidate)
            self._acks = candidate


def commit_persona_install(
    *, inspection: PersonaInspectionResult, acks: "PersonaInstallAckStore",
    personas_root: Path = Path("/config/personas"),
) -> "PersonaPack":
    import os
    import shutil

    from persona_pack import load_persona_pack

    identity = persona_install_consent_identity(
        persona_id=inspection.persona_id, version=inspection.version, checksum=inspection.checksum)
    if not acks.is_acked(identity):
        raise SpecialistInstallError(
            "consent_missing", "no recorded operator approval for this persona install")

    dest = personas_root / inspection.persona_id / inspection.version
    if not (dest / "manifest.json").is_file():
        # Round-3 fix (finding #1): this was the ONE commit path in the
        # whole plan that copied inspection-time bytes straight into their
        # FINAL location with NO verification step at all — no reload, no
        # re-derived checksum, nothing between "operator approved this
        # checksum" and "these bytes are now live at `dest`". Mirror the
        # specialist pipeline: stage into a TEMP directory under
        # `personas_root`, reload + recompute the checksum from THOSE
        # staged bytes, compare to `inspection.checksum`, and only then
        # atomically `os.replace` the verified temp directory into `dest`.
        # A mismatch/failure leaves no partial or wrong-checksum content at
        # `dest`.
        staging_parent = personas_root / ".staging"
        staging_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging_dest = staging_parent / uuid.uuid4().hex
        staging_dest.mkdir(parents=True, mode=0o700)
        try:
            shutil.copytree(inspection.staged_dir / "pack", staging_dest / "pack")
            shutil.copy2(inspection.staged_dir / "manifest.json", staging_dest / "manifest.json")
            staged_pack = load_persona_pack(staging_dest / "pack", staging_dest / "manifest.json")
            if (staged_pack.persona_id != inspection.persona_id
                    or staged_pack.version != inspection.version
                    or staged_pack.checksum != inspection.checksum):
                raise SpecialistInstallError(
                    "checksum_changed",
                    "staged persona no longer matches the approved inspection")
        except Exception:
            shutil.rmtree(staging_dest, ignore_errors=True)
            raise
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_dest, dest)
    return load_persona_pack(dest / "pack", dest / "manifest.json")


def apply_persona_override(
    *, target_role_id: str, persona: "PersonaPack", role: "RoleSlot", instance_dir_root: Path,
) -> Any:
    """Generalizes Task 8's resident_persona_swap for ANY persona-bearing
    agent — reuses check_persona_requirements + materialize_override_binding
    + InstanceDir exactly as Task 8 does; residents pass
    instance_dir_root=BINDINGS_ROOT/f"resident-{role.slot}",
    installed specialists pass SPECIALISTS_ROOT/role.slot (the
    SAME InstanceDir tree Task N1b-ii already writes to).

    Round-2 fix (finding #4): `InstanceTuple.root` means DIFFERENT things for
    the two tiers. For a resident it is a free-form descriptive label — role
    artifacts always load from the fixed image tree, never from `root` —
    so Plan 1's own resident_persona_swap already sets root=override_source
    and that is fine, unchanged here. For a specialist, `root` is
    STRUCTURALLY PARSED by activate_binding_for_config
    (parse_component_root) to locate the component's role artifact AND its
    bundled default persona in the CAS store — overwriting it with a bare
    persona ref (no "#sha256:..." suffix) makes parse_component_root raise
    ValueError on the very next load, and silently drops the existing
    config_snapshot/dependency_digests. A specialist override must keep
    `root` pointed at the component and carry the override ELSEWHERE on the
    binding (mode="override" + override_source — BindingRecord already has
    both fields; this function only needed to stop clobbering `root`)."""
    from personality_binding import (
        InstanceDir, InstanceTuple, check_persona_requirements, materialize_override_binding,
    )

    check_persona_requirements(role.normalized, persona)
    override_source = f"{persona.persona_id}@{persona.version}"
    instance_dir = InstanceDir(instance_dir_root)

    if role.kind != "specialist":
        # Resident path — UNCHANGED from the original draft, matches Plan 1's
        # own resident_persona_swap byte-for-byte (root is descriptive only).
        binding = materialize_override_binding(
            role=role, persona=persona, override_source=override_source)
        instance_dir.stage_desired(InstanceTuple(
            root=override_source, binding=binding, config_snapshot={},
            config_digest=binding.effective_config_digest,
        ))
        return instance_dir.commit_desired_to_active()

    # Specialist path — root MUST stay the component root; config/dependency
    # state carries forward from whatever is currently active.
    active_before = instance_dir.active()
    if active_before is None:
        raise SpecialistInstallError(
            "no_active_tuple",
            f"{target_role_id!r} has no active installed component to apply an override to")
    # materialize_override_binding (Plan 1 Task 7) hard-defaults
    # dependency_digests=()/EMPTY_CONFIG_DIGEST — correct for a resident (no
    # dependency closure exists there) but wrong for a specialist, whose
    # existing dependency/config state must survive an override swap
    # unchanged. Round-2 (finding #4) extends the Plan 1 signature with two
    # optional keyword-only params (already landed in N1c — see
    # personality_binding.materialize_override_binding), additive and
    # defaulted so every existing resident call site is unaffected.
    binding = materialize_override_binding(
        role=role, persona=persona, override_source=override_source,
        dependency_digests=active_before.binding.dependency_digests,
        effective_config_digest=active_before.binding.effective_config_digest,
    )
    instance_dir.stage_desired(InstanceTuple(
        root=active_before.root,                       # UNCHANGED — still the component root
        binding=binding,
        config_snapshot=active_before.config_snapshot,  # UNCHANGED — override never touches config
        config_digest=active_before.config_digest,
    ))
    return instance_dir.commit_desired_to_active()
