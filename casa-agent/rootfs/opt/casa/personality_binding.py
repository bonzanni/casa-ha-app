# casa-agent/rootfs/opt/casa/personality_binding.py
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

import jsonschema
import yaml

from canonical_bytes import checksum_json
from persona_pack import PersonaPack
from role_slot import (  # noqa: F401 — re-exported for existing callers (Task 6 owns these)
    EMPTY_CONFIG_DIGEST,
    RoleSlot,
    compute_effective_config_digest,
)
from trait_renderer import RENDERER_VERSION

_SCHEMA_DIR = Path(__file__).parent / "defaults" / "schema"

# NOTE: EMPTY_CONFIG_DIGEST / compute_effective_config_digest are defined in
# role_slot.py (Task 6), imported and re-exported here — NOT redefined. Task 6's
# own executor-loading wiring needs this constant BEFORE personality_binding.py
# exists under fresh-implementer, task-by-task execution; defining it twice would
# also violate this plan's "defined EXACTLY ONCE" rule (see Self-Review). Any test
# or caller that does `from personality_binding import EMPTY_CONFIG_DIGEST` keeps
# working unchanged — only the module that OWNS the value moved.


@dataclass(frozen=True, slots=True)
class BindingRecord:
    stable_agent_id: str
    role_checksum: str
    mode: Literal["image-default", "component-default", "override"]
    persona_id: str
    persona_version: str
    persona_checksum: str
    compiler_schema_version: str
    dependency_digests: tuple[str, ...]
    effective_config_digest: str
    binding_digest: str
    image_default_root: str | None = None
    component_root: str | None = None
    override_source: str | None = None


def compute_binding_digest(
    *, stable_agent_id: str, role_checksum: str, persona_id: str, persona_version: str,
    persona_checksum: str, compiler_schema_version: str, dependency_digests: tuple[str, ...],
    effective_config_digest: str,
) -> str:
    return checksum_json({
        "stable_agent_id": stable_agent_id,
        "role_checksum": role_checksum,
        "persona_id": persona_id,
        "persona_version": persona_version,
        "persona_checksum": persona_checksum,
        "compiler_schema_version": compiler_schema_version,
        "dependency_digests": sorted(dependency_digests),
        "effective_config_digest": effective_config_digest,
    })


def _build(
    *, role: RoleSlot, persona: PersonaPack, mode: str,
    dependency_digests: tuple[str, ...] = (), effective_config_digest: str = EMPTY_CONFIG_DIGEST,
    image_default_root: str | None = None, component_root: str | None = None,
    override_source: str | None = None,
) -> BindingRecord:
    digest = compute_binding_digest(
        stable_agent_id=role.role_id, role_checksum=role.checksum,
        persona_id=persona.persona_id, persona_version=persona.version,
        persona_checksum=persona.checksum,
        compiler_schema_version=RENDERER_VERSION,
        dependency_digests=dependency_digests, effective_config_digest=effective_config_digest,
    )
    return BindingRecord(
        stable_agent_id=role.role_id, role_checksum=role.checksum, mode=mode,
        persona_id=persona.persona_id, persona_version=persona.version,
        persona_checksum=persona.checksum,
        compiler_schema_version=RENDERER_VERSION,
        dependency_digests=tuple(sorted(dependency_digests)),
        effective_config_digest=effective_config_digest, binding_digest=digest,
        image_default_root=image_default_root, component_root=component_root,
        override_source=override_source,
    )


def materialize_image_default_binding(
    *, role: RoleSlot, persona: PersonaPack, image_default_root: str,
) -> BindingRecord:
    if role.kind != "resident":
        raise ValueError("image-default binding is resident-only")
    return _build(role=role, persona=persona, mode="image-default", image_default_root=image_default_root)


def materialize_override_binding(
    *, role: RoleSlot, persona: PersonaPack, override_source: str,
) -> BindingRecord:
    if role.kind not in {"resident", "specialist"}:
        raise ValueError("override binding is resident- or specialist-only")
    return _build(role=role, persona=persona, mode="override", override_source=override_source)


def _raw_from_binding(record: BindingRecord) -> dict[str, object]:
    return {
        "api_version": "casa.binding/v1",
        "stable_agent_id": record.stable_agent_id, "role_checksum": record.role_checksum,
        "mode": record.mode, "persona_id": record.persona_id,
        "persona_version": record.persona_version, "persona_checksum": record.persona_checksum,
        "compiler_schema_version": record.compiler_schema_version,
        "dependency_digests": list(record.dependency_digests),
        "effective_config_digest": record.effective_config_digest,
        "binding_digest": record.binding_digest,
        "image_default_root": record.image_default_root,
        "component_root": record.component_root, "override_source": record.override_source,
    }


def verify_binding_record(raw: dict) -> BindingRecord:
    """The ONE shared verification path: recompute the digest from every OTHER
    field and reject a mismatch. Both load_binding and InstanceDir's tuple loader
    call this — a binding's on-disk integrity is checked in exactly one place.

    Also schema-validates ``raw`` against binding.v1.json before field access.
    This is what turns a tampered/malformed nested binding (e.g. inside an
    instance tuple, where the outer schema only checks ``binding`` is an
    object) into a typed ``ValueError`` instead of a bare ``KeyError`` —
    instance-tuple.v1.json does not itself enforce the nested binding's
    required fields or patterns, so this is the only place that does for the
    nested case."""
    schema = json.loads((_SCHEMA_DIR / "binding.v1.json").read_text(encoding="utf-8"))
    try:
        jsonschema.validate(raw, schema)
    except jsonschema.ValidationError as exc:
        raise ValueError(str(exc)) from exc
    record = BindingRecord(
        stable_agent_id=raw["stable_agent_id"], role_checksum=raw["role_checksum"],
        mode=raw["mode"], persona_id=raw["persona_id"], persona_version=raw["persona_version"],
        persona_checksum=raw["persona_checksum"],
        compiler_schema_version=raw["compiler_schema_version"],
        dependency_digests=tuple(raw.get("dependency_digests") or ()),
        effective_config_digest=raw["effective_config_digest"],
        binding_digest=raw["binding_digest"],
        image_default_root=raw.get("image_default_root"),
        component_root=raw.get("component_root"), override_source=raw.get("override_source"),
    )
    expected = compute_binding_digest(
        stable_agent_id=record.stable_agent_id, role_checksum=record.role_checksum,
        persona_id=record.persona_id, persona_version=record.persona_version,
        persona_checksum=record.persona_checksum,
        compiler_schema_version=record.compiler_schema_version,
        dependency_digests=record.dependency_digests,
        effective_config_digest=record.effective_config_digest,
    )
    if record.binding_digest != expected:
        raise ValueError("binding_digest does not match canonical binding inputs")
    return record


def load_binding(path: Path) -> BindingRecord:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    schema = json.loads((_SCHEMA_DIR / "binding.v1.json").read_text(encoding="utf-8"))
    jsonschema.validate(raw, schema)
    try:
        return verify_binding_record(raw)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def atomic_write_binding(path: Path, record: BindingRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = yaml.safe_dump(_raw_from_binding(record), sort_keys=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


@dataclass(frozen=True, slots=True)
class InstanceTuple:
    root: str
    binding: BindingRecord
    config_snapshot: Mapping[str, object]
    config_digest: str


def verify_instance_tuple(raw: dict) -> InstanceTuple:
    binding = verify_binding_record(raw["binding"])
    config_digest = raw["config_digest"]
    if config_digest != binding.effective_config_digest:
        raise ValueError("instance tuple config_digest does not match its binding's effective_config_digest")
    return InstanceTuple(
        root=raw["root"], binding=binding,
        config_snapshot=raw.get("config_snapshot") or {}, config_digest=config_digest,
    )


def _raw_from_tuple(tuple_: InstanceTuple) -> dict[str, object]:
    return {
        "api_version": "casa.instance-tuple/v1", "root": tuple_.root,
        "binding": _raw_from_binding(tuple_.binding),
        "config_snapshot": dict(tuple_.config_snapshot), "config_digest": tuple_.config_digest,
    }


def load_instance_tuple(path: Path) -> InstanceTuple | None:
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    schema = json.loads((_SCHEMA_DIR / "instance-tuple.v1.json").read_text(encoding="utf-8"))
    jsonschema.validate(raw, schema)
    try:
        return verify_instance_tuple(raw)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def atomic_write_instance_tuple(path: Path, tuple_: InstanceTuple) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = yaml.safe_dump(_raw_from_tuple(tuple_), sort_keys=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class InstanceDir:
    """One persona-bearing agent instance's on-disk active/desired/prior tuple
    pair (spec §4.1). Residents (Task 8) use
    ``/config/bindings/resident-<slot>/``; the specialist data model (Task 13)
    uses ``/config/specialists/<slug>/`` — SAME file format, SAME code, reused
    verbatim by Plan 2's N1 for install/upgrade/rollback."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, name: str) -> Path:
        return self._dir / name

    def active(self) -> InstanceTuple | None:
        return load_instance_tuple(self._path("active.yaml"))

    def desired(self) -> InstanceTuple | None:
        return load_instance_tuple(self._path("desired.yaml"))

    def stage_desired(self, tuple_: InstanceTuple) -> None:
        atomic_write_instance_tuple(self._path("desired.yaml"), tuple_)

    def commit_desired_to_active(self) -> InstanceTuple:
        desired_path = self._path("desired.yaml")
        candidate = load_instance_tuple(desired_path)
        if candidate is None:
            raise ValueError(f"{self._dir}: no desired tuple staged to commit")
        active_path = self._path("active.yaml")
        prior_path = self._path("active.prior.yaml")
        current_active = load_instance_tuple(active_path)
        if current_active is not None and current_active.binding.binding_digest == candidate.binding.binding_digest:
            # Crash-retry / no-op recommit (§4.1): a previous run already wrote
            # `candidate` to active.yaml but died before unlinking desired.yaml.
            # active.yaml already IS the candidate, so do NOT rotate prior again
            # — that would overwrite the true pre-commit rollback target with a
            # duplicate of the new active. Just finish the interrupted step.
            desired_path.unlink(missing_ok=True)
            return candidate
        if active_path.exists():
            os.replace(self._copy_to_temp(active_path), prior_path)
        atomic_write_instance_tuple(active_path, candidate)  # write new active LAST-safe
        desired_path.unlink(missing_ok=True)
        return candidate

    def _copy_to_temp(self, path: Path) -> Path:
        temp = path.with_suffix(path.suffix + ".rollback-tmp")
        temp.write_bytes(path.read_bytes())
        os.chmod(temp, 0o600)
        return temp

    def discard_desired(self, *, reason: str) -> None:
        desired_path = self._path("desired.yaml")
        if not desired_path.exists():
            return
        error_path = self._path("desired.error.yaml")
        payload = yaml.safe_load(desired_path.read_text(encoding="utf-8")) or {}
        payload["_error_reason"] = reason
        error_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        desired_path.unlink()
