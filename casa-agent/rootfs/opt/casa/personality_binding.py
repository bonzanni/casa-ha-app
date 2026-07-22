# casa-agent/rootfs/opt/casa/personality_binding.py
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Literal, Mapping

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
    dependency_digests: tuple[str, ...] = (), effective_config_digest: str = EMPTY_CONFIG_DIGEST,
) -> BindingRecord:
    """Task N1c extension (Controller resolution #1): `dependency_digests`/
    `effective_config_digest` are additive optional kwargs, passed straight
    through to `_build` (which already accepts both — every OTHER binding
    mode already threads them). Existing resident callers
    (`reconcile_resident_binding` above, `tools.py resident_persona_swap`)
    pass neither and get byte-identical behavior: `_build`'s own defaults
    for these two params are these exact same values, so the digest and
    every other field are unchanged for a resident override. This lets
    `upgrade_specialist`/`rollback_specialist` (specialist_install.py)
    preserve an OVERRIDE-bound specialist's persona pin across an upgrade
    while still capturing the new component's dependency closure and the
    operator's re-validated config in the binding digest, exactly the way
    `materialize_component_default_binding` already does for the
    component-default mode."""
    if role.kind not in {"resident", "specialist"}:
        raise ValueError("override binding is resident- or specialist-only")
    return _build(
        role=role, persona=persona, mode="override", override_source=override_source,
        dependency_digests=dependency_digests, effective_config_digest=effective_config_digest,
    )


def materialize_component_default_binding(
    *, role: RoleSlot, persona: PersonaPack, component_root: str,
    dependency_digests: tuple[str, ...] = (), effective_config_digest: str = EMPTY_CONFIG_DIGEST,
) -> BindingRecord:
    """Spec §2.3's third binding mode — specialist-only, tracks the default
    persona pinned by the INSTALLED component (as opposed to image-default,
    which tracks the image's own default, or override, which pins an exact
    operator-chosen digest). Reuses the SAME `_build` helper Task 7's other
    two materializers use — one binding-construction path, three modes."""
    if role.kind != "specialist":
        raise ValueError("component-default binding is specialist-only")
    return _build(
        role=role, persona=persona, mode="component-default", component_root=component_root,
        dependency_digests=dependency_digests, effective_config_digest=effective_config_digest,
    )


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
        # Task N1c fix: compare the FULL tuple (root included), not just
        # binding_digest. binding_digest deliberately excludes `root`
        # (compute_binding_digest's eight normative fields never include it
        # — see test_digest_input_set_matches_the_normative_eight_fields),
        # so two installed-component versions that bump only the manifest
        # version (role.yaml/doctrine.md/persona/dependency-digest bytes all
        # unchanged) share the SAME binding_digest while their `root`
        # (component_id@version#checksum) genuinely differs — upgrade_
        # specialist's own test_upgrade_commits_a_new_active_tuple_and_
        # retains_the_prior_as_rollback_target exercises exactly this case.
        # Under the old binding_digest-only check that scenario was
        # mis-detected as a crash-retry no-op and silently skipped writing
        # the new (different-root) active.yaml at all. Both `candidate` and
        # `current_active` are round-tripped through the SAME
        # load_instance_tuple/verify_instance_tuple path, so this equality
        # is a fair, symmetric comparison — the true crash-retry case (this
        # exact tuple, root included, already written to active.yaml before
        # a crash) is unaffected: it is still recognized and short-circuits
        # exactly as before (see test_commit_is_crash_retry_idempotent_and_
        # preserves_true_prior).
        if current_active is not None and current_active == candidate:
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


# --- Resident persona defaults + boot-time reconciliation (Task 8) ----------

# The ONLY code path that chooses a fixed resident slot's default persona ref.
# Keyed by role_slot.FIXED_RESIDENT_SLOTS; each value is an exact
# "<namespace>/<slug>@<version>" ref resolvable under defaults/personas/.
IMAGE_DEFAULT_PERSONA_BY_SLOT: Mapping[str, str] = MappingProxyType({
    "assistant": "casa/ellen@0.1.0",
    "butler": "casa/tina@0.1.0",
    "concierge": "casa/gary@0.1.0",
})

# A persona_requirements entry is either an exact "ns/slug@X.Y.Z" pin (matched
# by string equality) or a "ns/slug@>=X.Y.Z <A.B.C" range; a "ns/*@..." pattern
# matches any slug in that namespace.
_RANGE_RE = re.compile(
    r"^(?P<ns_slug>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?/[a-z0-9*][a-z0-9-]*)@"
    r">=(?P<low>\d+\.\d+\.\d+)\s*<(?P<high>\d+\.\d+\.\d+)$"
)


def _semver_tuple(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def check_persona_requirements(role: Mapping[str, object], persona: PersonaPack) -> None:
    """Validate persona.compatibility (the role's optional persona_requirements
    constraint, spec §2.3): each entry is either an exact 'ns/slug@X.Y.Z' pin or a
    'ns/slug@>=X.Y.Z <A.B.C' range; 'ns/*@...' matches any slug in that namespace.
    A role with persona.policy == 'forbidden' has no compatibility list to check."""
    persona_block = role.get("persona", {}) or {}
    if persona_block.get("policy") == "forbidden":
        return
    entries = persona_block.get("compatibility") or ()
    ref = f"{persona.persona_id}@{persona.version}"
    for entry in entries:
        if entry == ref:
            return
        match = _RANGE_RE.match(entry)
        if not match:
            continue
        namespace, _, slug_pattern = match.group("ns_slug").partition("/")
        persona_namespace, _, persona_slug = persona.persona_id.partition("/")
        if namespace != persona_namespace or (slug_pattern != "*" and slug_pattern != persona_slug):
            continue
        low, high = _semver_tuple(match.group("low")), _semver_tuple(match.group("high"))
        if low <= _semver_tuple(persona.version) < high:
            return
    raise ValueError(
        f"persona {ref} does not satisfy role {role.get('id')}'s persona_requirements {list(entries)}"
    )


def reconcile_resident_binding(
    *, role: RoleSlot, image_default_persona_loader: Callable[[str], PersonaPack],
    override_persona_loader: Callable[[str], PersonaPack], instance_dir: InstanceDir,
) -> InstanceTuple:
    """Boot-time reconciliation of a resident's binding (spec §4.1, §4.2, §4.4).

    Reads an already-staged ``desired.yaml`` FIRST — the artifact
    ``resident_persona_swap``/``resident_persona_reset`` write BEFORE a restart —
    and, when present, THAT staged candidate's persona selection is what gets
    validated/compiled/committed (spec §4.2 step 4: "restart the affected agent;
    on success active := desired"). Without this, a swap/reset staged before the
    restart would be silently discarded and the resident would boot back onto its
    old binding.

    Either way — staged swap/reset, or the passive image-default-tracking path
    when nothing is staged — the candidate binding is ALWAYS recomputed against
    the role CURRENTLY loading (never a stale stored role_checksum), so an image
    upgrade to role.yaml landing in the same restart as a pending swap still gets
    the current role_checksum (spec §4.4).

    1. Determine the candidate's persona SELECTION (mode + persona ref):
       - a staged ``desired.yaml``, if present, wins;
       - otherwise an override-bound ACTIVE tuple keeps its exact pinned persona;
       - otherwise (image-default binding, or no active tuple at all — fresh
         install) resolve the CURRENT ``IMAGE_DEFAULT_PERSONA_BY_SLOT[role.slot]``.
    2. Materialize the candidate binding against the CURRENT role.
    3. If the candidate's binding_digest equals the active tuple's, this is a
       no-op — return the active tuple unchanged, discarding any now-redundant
       staged file.
    4. Otherwise (re-)stage the candidate as desired, validate persona↔role
       compatibility, and on success commit ``active := desired`` via
       ``InstanceDir.commit_desired_to_active()``. On failure (persona blob
       missing/incompatible, disk error), discard the desired candidate with a
       diagnostic and return the RETAINED PRIOR active tuple — boot proceeds on
       the last-known-good binding, never crash-looping.
    5. Only when there is NO active tuple at all (fresh install) AND step 4 fails
       does this hard-fail loudly — raise ValueError so the caller turns it into
       an actionable LoadError.

    The persona resolve/materialize calls run INSIDE the same guarded block as
    validate/stage/commit, so every failure mode is caught by the SAME handler
    and ``active`` is preserved whenever an active tuple exists.
    """
    active = instance_dir.active()
    staged = instance_dir.desired()

    source_binding = staged.binding if staged is not None else (
        active.binding if active is not None and active.binding.mode == "override" else None
    )
    try:
        if source_binding is not None and source_binding.mode == "override":
            persona_ref = f"{source_binding.persona_id}@{source_binding.persona_version}"
            persona = override_persona_loader(persona_ref)
            candidate_binding = materialize_override_binding(
                role=role, persona=persona, override_source=source_binding.override_source,
            )
            root = candidate_binding.override_source
        else:
            default_ref = IMAGE_DEFAULT_PERSONA_BY_SLOT[role.slot]
            persona = image_default_persona_loader(default_ref)
            candidate_binding = materialize_image_default_binding(
                role=role, persona=persona, image_default_root=default_ref,
            )
            root = candidate_binding.image_default_root

        if active is not None and active.binding.binding_digest == candidate_binding.binding_digest:
            if staged is not None:
                instance_dir.discard_desired(reason="no-op: candidate matches the already-active binding")
            return active

        candidate_tuple = InstanceTuple(
            root=root, binding=candidate_binding, config_snapshot={},
            config_digest=candidate_binding.effective_config_digest,
        )
        check_persona_requirements(role.normalized, persona)
        instance_dir.stage_desired(candidate_tuple)
        return instance_dir.commit_desired_to_active()
    except (ValueError, OSError) as exc:
        # discard_desired() is a no-op when nothing was ever staged (e.g. the
        # persona loader itself raised before stage_desired ran).
        instance_dir.discard_desired(reason=str(exc))
        if active is None:
            raise ValueError(
                f"resident {role.role_id}: no prior active binding exists and the "
                f"fresh reconciliation failed: {exc}"
            ) from exc
        return active
