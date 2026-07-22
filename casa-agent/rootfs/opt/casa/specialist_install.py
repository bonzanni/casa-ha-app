from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping

from canonical_bytes import reject_forbidden_markers, to_plain_json
from specialist_component import SpecialistComponent, load_specialist_component
from specialist_lifecycle import check_slug_uniqueness

if TYPE_CHECKING:
    from specialist_install_consent import SpecialistInstallAckStore
    from specialist_lifecycle import SpecialistInstance
    from specialist_registry import InstalledSpecialistIndex

logger = logging.getLogger(__name__)


class SpecialistInstallError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.detail = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DependencyResolution:
    kind: str
    identifier: str
    digest: str
    available: bool
    detail: str


@dataclass(frozen=True, slots=True)
class InspectionResult:
    component_id: str
    version: str
    slug: str
    component_checksum: str
    root_digest: str          # Round-2 addition (finding #2) — see compute_install_root_digest
    mission: str
    default_persona_ref: str
    default_persona_checksum: str
    required_config_names: tuple[str, ...]
    required_secret_names: tuple[str, ...]
    dependencies: tuple[DependencyResolution, ...]
    staged_dir: Path


def compute_install_root_digest(
    component: "SpecialistComponent", dependencies: tuple[DependencyResolution, ...],
    *, manifest_bytes: bytes,
) -> str:
    """Round-2 fix (finding #2): `component.checksum` (Plan 1 Task 13) only
    covers role.yaml/doctrine.md/config-schema.json — NOT manifest.json
    itself, NOT the bundled persona pack, NOT corpus bytes, NOT the pinned
    plugin digest. Operator consent and CAS addressing must attest to the
    FULL closure, not a 3-file subset. This is the identity `commit_
    specialist_install`/`upgrade_specialist` bind consent AND the CAS
    directory name to; it is ALWAYS recomputed fresh from re-loaded bytes,
    never trusted from a caller-supplied field."""
    from canonical_bytes import checksum_bytes, checksum_json

    return checksum_json({
        "component_checksum": component.checksum,
        "manifest_checksum": checksum_bytes(manifest_bytes),
        "dependency_digests": sorted(d.digest for d in dependencies),
    })


def resolve_and_fetch(
    repo: str, ref: str, subdir: str, dest: Path, *, expected_revision: str | None = None,
) -> str:
    """Resolve *ref* to a commit sha (guarding against a moved tag exactly
    like `plugin_add`'s `_resolve_and_guard`, tools.py) then fetch that
    EXACT commit's subtree — never a mutable branch fetch. Raises
    SpecialistInstallError on any resolve/fetch failure; never partially
    populates *dest* on failure (fetch_commit_tree extracts to a temp dir
    first)."""
    import plugin_store

    try:
        commit = plugin_store.resolve_ref(repo, ref)
    except plugin_store.RefNotFound as exc:
        raise SpecialistInstallError("ref_not_found", str(exc)) from exc
    except plugin_store.ResolveAuthFailed as exc:
        raise SpecialistInstallError("resolve_auth_failed", str(exc)) from exc
    except plugin_store.SourceEmpty as exc:
        raise SpecialistInstallError("source_empty", str(exc)) from exc
    except plugin_store.ResolveUnavailable as exc:
        raise SpecialistInstallError("resolve_unavailable", str(exc)) from exc
    if expected_revision is not None:
        want = plugin_store.normalize_revision(expected_revision)
        if want is None or want != commit:
            raise SpecialistInstallError(
                "revision_mismatch",
                f"expected_revision {expected_revision!r} does not match resolved "
                f"commit {commit!r} for {repo}@{ref}",
            )
    try:
        plugin_store.fetch_commit_tree(repo, commit, subdir, dest, timeout=300.0)
    except plugin_store.StoreError as exc:
        raise SpecialistInstallError(
            getattr(exc, "reason_code", "fetch_failed"), str(exc)) from exc
    return commit


def resolve_dependency_closure(
    component: SpecialistComponent, component_dir: Path,
) -> tuple[DependencyResolution, ...]:
    """Resolve every typed dependency's AVAILABILITY (spec §2.4). Convention
    (this plan, "Component repository layout"): `persona` and `corpus/data`
    dependencies are bundled INSIDE the component repo (`persona/`,
    `corpus/<identifier>/`) so a fresh install never depends on the TARGET
    image already having a matching blob; `plugin/implementation`
    dependencies reference an ALREADY plugin_add-installed plugin (a
    component never bundles plugin code, only pins which published artifact
    digest it was validated against)."""
    import plugin_registry
    from persona_pack import PersonaPackError, load_persona_pack
    from plugin_store import content_checksum

    out: list[DependencyResolution] = []
    for dep in component.dependencies:
        if dep.kind == "persona":
            pack_dir = component_dir / "persona" / "pack"
            manifest_path = component_dir / "persona" / "manifest.json"
            if not pack_dir.is_dir() or not manifest_path.is_file():
                out.append(DependencyResolution(
                    kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                    available=False, detail="bundled persona/ directory is missing"))
                continue
            try:
                pack = load_persona_pack(pack_dir, manifest_path)
            except PersonaPackError as exc:
                out.append(DependencyResolution(
                    kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                    available=False, detail=f"bundled persona invalid: {exc}"))
                continue
            available = pack.checksum == dep.digest
            out.append(DependencyResolution(
                kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                available=available,
                detail="" if available else "bundled persona checksum does not match manifest"))
        elif dep.kind == "corpus/data":
            corpus_dir = component_dir / "corpus" / dep.identifier
            if not corpus_dir.is_dir():
                out.append(DependencyResolution(
                    kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                    available=False, detail=f"bundled corpus/{dep.identifier}/ is missing"))
                continue
            # Round-2 fix (finding #6): plugin_store.content_checksum returns a
            # BARE hex digest; ComponentDependency.digest is schema-constrained
            # to `sha256:<hex>` (specialist-component.v1.json). Comparing the
            # bare form directly against the prefixed field can never match —
            # normalize here, the ONE place a bare content_checksum() result
            # crosses into a sha256:-prefixed digest field.
            digest = "sha256:" + content_checksum(corpus_dir)
            available = digest == dep.digest
            out.append(DependencyResolution(
                kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                available=available,
                detail="" if available else "bundled corpus checksum does not match manifest"))
        elif dep.kind == "plugin/implementation":
            # Round-2 fix (finding #6): a registry entry's `artifact_id` is
            # `plugin_registry.compute_artifact_id` — sha256(repo + "\n" +
            # revision + "\n" + subdir + "\n" + name), an IDENTITY hash of the
            # plugin's SOURCE COORDINATES. It is never equal to, and shares no
            # meaningful relationship with, a content checksum — the previous
            # `artifact_id.endswith(digest_suffix)` comparison could never
            # match two independently-computed 64-hex strings by construction.
            # The REAL way to verify an installed plugin's CONTENT: resolve it
            # through plugin_registry (which already deep-validates identity +
            # stored content_checksum via plugin_store.artifact_verdict at
            # snapshot-build time) and hash its on-disk artifact directory the
            # same way plugin_store always does.
            resolved = next(
                (p for p in plugin_registry.resolve_all().plugins
                 if p.name == dep.identifier), None,
            )
            if resolved is None:
                out.append(DependencyResolution(
                    kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                    available=False, detail=(
                        f"plugin {dep.identifier!r} is not registered/valid — "
                        f"plugin_add it first")))
                continue
            current_digest = "sha256:" + content_checksum(Path(resolved.path))
            available = current_digest == dep.digest
            out.append(DependencyResolution(
                kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                available=available,
                detail="" if available else (
                    f"plugin {dep.identifier!r} is installed but its current content checksum "
                    f"does not match the pinned digest — re-publish or re-plugin_add it")))
        else:
            out.append(DependencyResolution(
                kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                available=False, detail=f"unknown dependency kind {dep.kind!r}"))
    return tuple(out)


def _validate_untrusted_bytes(component: SpecialistComponent) -> None:
    """Extra check role_artifact.load_role_artifact does not perform: reject
    templating/HTML/delimiter markers in the FETCHED role.yaml/doctrine.md,
    which are adversarial input, unlike image-owned role artifacts."""
    import yaml

    role_text = component.role.role_path.read_text(encoding="utf-8")
    try:
        reject_forbidden_markers(role_text)
        reject_forbidden_markers(component.role.doctrine)
    except ValueError as exc:
        raise SpecialistInstallError("forbidden_markers", str(exc)) from exc
    # Belt-and-suspenders: re-serializing role.yaml must not silently absorb
    # a marker that only appears in a value jsonschema doesn't visit.
    # component.role.role is role_artifact.load_role_artifact's
    # canonical_bytes.deep_freeze()-produced tree (nested dict -> MappingProxyType,
    # list -> tuple) — comparing it directly against yaml.safe_load's plain
    # dict/list tree would spuriously mismatch on every list-valued field
    # (list != tuple always, even when every element is equal). Normalize
    # through to_plain_json first so this is a genuine structural-drift
    # check, not a frozen-container-type false positive.
    reparsed = yaml.safe_load(role_text)
    if reparsed != to_plain_json(component.role.role):
        raise SpecialistInstallError(
            "role_artifact_drift", "role.yaml on disk does not match the loaded artifact")


def inspect_specialist_repo(
    repo: str, ref: str, *, subdir: str = "", expected_revision: str | None = None,
    staging_root: Path = Path("/config/specialists/.staging"),
    installed_index: "InstalledSpecialistIndex | None" = None,
    mode: "Literal['install', 'upgrade']" = "install",
    target_slug: str | None = None,
    specialists_dir: Path = Path("/config/specialists"),
) -> InspectionResult:
    """Fetch for inspection into a NON-PERSISTENT staging directory (spec §6
    N1) — no CAS write, no binding, no activation. Every check that can
    reject an install runs here, BEFORE any operator is ever prompted.

    Round-2 fix (finding #5): `mode="upgrade"` (with a required `target_slug`)
    is the ONLY sanctioned way to inspect a repo for a slug that is ALREADY
    installed — plain `mode="install"` (the default, used for a fresh
    install) always applies the full collision check, so re-inspecting an
    already-installed slug in install mode correctly still fails
    (`check_slug_uniqueness` sees it in `installed_specialist_slugs`) —
    upgrade mode does not weaken that for any OTHER slug, it narrowly
    excludes only `target_slug` after independently confirming an active
    instance of that exact slug already exists (never usable to backdoor a
    fresh install past collision checks under a false 'upgrade' claim)."""
    from specialist_registry import InstalledSpecialistIndex, _discover_image_role_slots

    if mode == "upgrade" and not target_slug:
        raise SpecialistInstallError("target_slug_required", "mode='upgrade' requires target_slug")

    staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    component_dir = staging_root / uuid.uuid4().hex
    resolve_and_fetch(repo, ref, subdir, component_dir, expected_revision=expected_revision)

    manifest_path = component_dir / "manifest.json"
    if not manifest_path.is_file():
        raise SpecialistInstallError("manifest_missing", f"{repo}@{ref}: manifest.json not found")
    try:
        component = load_specialist_component(component_dir, manifest_path)
    except ValueError as exc:
        raise SpecialistInstallError("manifest_invalid", str(exc)) from exc

    _validate_untrusted_bytes(component)

    index = installed_index or InstalledSpecialistIndex()
    if installed_index is None:
        index.load()

    if mode == "upgrade":
        if component.slug != target_slug:
            raise SpecialistInstallError(
                "slug_mismatch",
                f"upgrade target_slug={target_slug!r} but the fetched component declares "
                f"slug={component.slug!r} — a slug rename is a fresh install, not an upgrade")
        from personality_binding import InstanceDir
        if InstanceDir(specialists_dir / target_slug).active() is None:
            raise SpecialistInstallError(
                "no_active_tuple", f"{target_slug!r} has no active install to upgrade")
        fixed_role_slots = _discover_image_role_slots() - {target_slug}
        installed_specialist_slugs = index.installed_slugs() - {target_slug}
    else:
        fixed_role_slots = _discover_image_role_slots()
        installed_specialist_slugs = index.installed_slugs()

    try:
        check_slug_uniqueness(
            candidate_slug=component.slug,
            fixed_role_slots=fixed_role_slots,
            installed_specialist_slugs=installed_specialist_slugs,
        )
    except ValueError as exc:
        raise SpecialistInstallError("slug_collision", str(exc)) from exc

    dependencies = resolve_dependency_closure(component, component_dir)
    unavailable = [d for d in dependencies if not d.available]
    if unavailable:
        detail = "; ".join(f"{d.kind}:{d.identifier}: {d.detail}" for d in unavailable)
        raise SpecialistInstallError("dependency_unavailable", detail)

    root_digest = compute_install_root_digest(
        component, dependencies, manifest_bytes=manifest_path.read_bytes())

    required = component.config_schema.get("required", [])
    secret_names = set(component.config_schema.get("secret_names", []))
    logger.info(
        "inspect_specialist_repo passed all gates: mode=%s slug=%s component_id=%s "
        "version=%s root_digest=%s (staged at %s, not yet activated)",
        mode, component.slug, component.component_id, component.version,
        root_digest, component_dir,
    )
    return InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest,
        mission=str(component.role.role.get("mission", "")),
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=tuple(n for n in required if n not in secret_names),
        required_secret_names=tuple(n for n in required if n in secret_names),
        dependencies=dependencies, staged_dir=component_dir,
    )


# ---------------------------------------------------------------------------
# CAS addressing (Step 11)
# ---------------------------------------------------------------------------
#
# The CAS store root is /config/specialists/store/<component_checksum-without-
# "sha256:"-prefix>/ (content-addressed, spec §2.5), holding the fetched
# component verbatim (role/, persona/, corpus/, config-schema.json,
# manifest.json) after validation. BindingRecord.component_root (Task 7's
# free-form str | None field) is set to
# f"{component_id}@{version}#{component_checksum}" — human-readable AND
# parseable, so InstalledSpecialistIndex can recover the CAS directory from a
# loaded active.yaml/desired.yaml without a second sidecar file.


def component_root_string(*, component_id: str, version: str, component_checksum: str) -> str:
    return f"{component_id}@{version}#{component_checksum}"


def parse_component_root(component_root: str) -> tuple[str, str, str]:
    """Inverse of component_root_string. Raises ValueError on a malformed root
    (never silently returns a partial tuple — a corrupt InstanceTuple's root
    must fail closed, not resolve to a guessed CAS path)."""
    head, sep, checksum = component_root.rpartition("#")
    if not sep or not checksum.startswith("sha256:"):
        raise ValueError(f"malformed component_root: {component_root!r}")
    component_id, sep2, version = head.rpartition("@")
    if not sep2:
        raise ValueError(f"malformed component_root: {component_root!r}")
    return component_id, version, checksum


def cas_store_dir(
    component_checksum: str, *, store_root: Path = Path("/config/specialists/store"),
) -> Path:
    return store_root / component_checksum.removeprefix("sha256:")


def commit_specialist_install(
    *, inspection: "InspectionResult", config: "Mapping[str, str]",
    secret_names_provided: frozenset[str], acks: "SpecialistInstallAckStore",
    specialists_dir: Path = Path("/config/specialists"),
    agents_specialists_dir: Path = Path("/config/agents/specialists"),
) -> "SpecialistInstance":
    """The ONLY function that writes into the CAS/specialists tree (spec §6
    N1: "consent precedes any persistent CAS install/activation"). Order:
    verify consent -> persist to CAS -> compile (persona↔role compatibility)
    -> stage the InstanceDir tuple as desired -> commit the tuple to active
    -> materialize the runtime files as a best-effort follow-up. Commit is
    skipped entirely for a pending-configuration candidate — an
    uninstantiable specialist must not appear loadable.

    Round-4 fix (this review pass, finding #2 — supersedes round 2's
    "materialize BEFORE commit" ordering below). `InstanceDir.
    commit_desired_to_active()` (Plan 1) is the single authoritative,
    atomically-written record — writing `active.yaml` via
    `atomic_write_instance_tuple` is itself a single `os.replace`-backed
    write, and re-running the whole method on a later boot is a documented
    safe no-op. The operational files this function materializes afterward
    are a DERIVED CACHE of that tuple, not a second source of truth, so
    committing first and materializing second (rather than the reverse) is
    safe: if materialize fails here (disk full, permission error, a racing
    uninstall), the failure is caught, logged, and surfaced as a non-fatal
    `last_activation_error` on the returned `SpecialistInstance` — the
    already-committed tuple is NOT rolled back, because
    `specialist_materialize.current_specialist_roles_dir` (threaded through
    every boot/`casa_reload` call site per Correction #1) unconditionally
    re-materializes every ACTIVE slug's operational files from its tuple on
    every subsequent call, so this slug self-heals on the very next
    reconcile with no operator action required."""
    from personality_binding import (
        InstanceDir, InstanceTuple, check_persona_requirements,
        compute_effective_config_digest, materialize_component_default_binding,
    )
    from persona_pack import load_persona_pack
    from prompt_compiler import compile_prompt_bundle
    from role_slot import materialize_role
    from role_artifact import load_role_artifact
    from specialist_lifecycle import SpecialistInstance, satisfy_config
    from specialist_component import load_specialist_component
    from specialist_install_consent import install_consent_identity
    import specialist_materialize

    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        component_checksum=inspection.root_digest, slug=inspection.slug,
    )
    if not acks.is_acked(identity):
        raise SpecialistInstallError(
            "consent_missing",
            f"no recorded operator approval for {inspection.component_id}@"
            f"{inspection.version} (root digest {inspection.root_digest})",
        )

    # Round-3 fix (finding #1 — CAS-before-verify): CAS addressing is keyed
    # by the FULL-CLOSURE root_digest, not the narrow component_checksum —
    # the operator's approval attests to the whole closure, so the CAS
    # directory identity must too. CRITICALLY, the copy from
    # `inspection.staged_dir` lands in a TEMPORARY staging directory first —
    # never directly at the final, content-addressed `cas_dir` — so a
    # digest mismatch below can never leave a wrong-digest-named CAS
    # directory behind (a poisoned CAS entry a later `cas_dir.exists()`
    # check for this SAME digest would then trust forever, since CAS
    # content is treated as immutable once present at its digest path).
    cas_dir = cas_store_dir(inspection.root_digest, store_root=specialists_dir / "store")
    if not cas_dir.exists():
        staging_root = specialists_dir / "store" / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        cas_staging_dir = staging_root / uuid.uuid4().hex
        shutil.copytree(inspection.staged_dir, cas_staging_dir, dirs_exist_ok=False, symlinks=True)
        for path in cas_staging_dir.rglob("*"):
            if path.is_file():
                path.chmod(0o400)
        try:
            # Reload + recompute the FULL dependency closure and root digest
            # from the STAGED (not-yet-CAS) bytes and compare to the
            # operator-acknowledged digest BEFORE this content is ever
            # visible under its content-addressed name. A mismatch discards
            # the staging dir and raises; `cas_dir` is never created for a
            # component that fails verification.
            staged_component = load_specialist_component(
                cas_staging_dir, cas_staging_dir / "manifest.json")
            staged_deps = resolve_dependency_closure(staged_component, cas_staging_dir)
            staged_unavailable = [d for d in staged_deps if not d.available]
            if staged_unavailable:
                detail = "; ".join(
                    f"{d.kind}:{d.identifier}: {d.detail}" for d in staged_unavailable)
                raise SpecialistInstallError("dependency_unavailable", detail)
            staged_root_digest = compute_install_root_digest(
                staged_component, staged_deps,
                manifest_bytes=(cas_staging_dir / "manifest.json").read_bytes())
            if staged_root_digest != inspection.root_digest:
                raise SpecialistInstallError(
                    "checksum_changed",
                    "staged component no longer matches the approved inspection")
        except Exception:
            shutil.rmtree(cas_staging_dir, ignore_errors=True)
            raise
        # Verified: atomically publish into the final content-addressed
        # location. `os.replace` is a single directory rename on the same
        # filesystem (/config always is) — no partially-written or
        # wrong-digest CAS state is ever observable at `cas_dir`.
        os.replace(cas_staging_dir, cas_dir)

    # Re-load from the now-final (or, for a digest an earlier install
    # already verified and published, pre-existing) CAS directory.
    component = load_specialist_component(cas_dir, cas_dir / "manifest.json")
    role = materialize_role(source=load_role_artifact(cas_dir / "role"), options={})
    persona = load_persona_pack(cas_dir / "persona" / "pack", cas_dir / "persona" / "manifest.json")

    # Re-run the FULL dependency closure against the final CAS path and
    # re-derive the root digest ONE more time, immediately before any tuple
    # is staged — this is the "reject unavailable/changed bytes immediately
    # before persistence/activation" gate. A dependency that flips
    # unavailable, or a root digest that no longer matches what the
    # operator acked, aborts here even though the bytes are already in CAS
    # (CAS content is immutable/content-addressed, so this can only happen
    # if `inspection` itself was stale/tampered, or the digest already
    # existed in CAS from a prior, still-valid install — never trust
    # `inspection` past this point).
    fresh_deps = resolve_dependency_closure(component, cas_dir)
    unavailable = [d for d in fresh_deps if not d.available]
    if unavailable:
        detail = "; ".join(f"{d.kind}:{d.identifier}: {d.detail}" for d in unavailable)
        raise SpecialistInstallError("dependency_unavailable", detail)
    fresh_root_digest = compute_install_root_digest(
        component, fresh_deps, manifest_bytes=(cas_dir / "manifest.json").read_bytes())
    if fresh_root_digest != inspection.root_digest:
        raise SpecialistInstallError(
            "checksum_changed", "CAS-persisted component no longer matches the approved inspection")

    satisfied, missing = satisfy_config(
        schema=component.config_schema, provided_non_secret=config,
        provided_secret_names=secret_names_provided,
    )
    root = component_root_string(
        component_id=component.component_id, version=component.version,
        component_checksum=fresh_root_digest,
    )
    instance_dir = InstanceDir(specialists_dir / inspection.slug)
    dependency_digests = tuple(sorted(d.digest for d in fresh_deps))

    if not satisfied:
        # Fail closed into pending-configuration: a desired candidate is
        # staged so the operator can see WHAT is missing, but nothing
        # activates and nothing materializes into the runtime load path.
        placeholder_binding = materialize_component_default_binding(
            role=role, persona=persona, component_root=root,
            dependency_digests=dependency_digests,
        )
        instance_dir.stage_desired(InstanceTuple(
            root=root, binding=placeholder_binding, config_snapshot=dict(config),
            config_digest=placeholder_binding.effective_config_digest,
        ))
        return SpecialistInstance(
            slug=inspection.slug, stable_agent_id=f"specialist:{inspection.slug}",
            state="pending-configuration", active=None, desired=instance_dir.desired(),
            last_activation_error=f"missing required config/secret: {missing}",
        )

    check_persona_requirements(role.normalized, persona)  # raises ValueError if incompatible
    effective_config_digest = compute_effective_config_digest(dict(config))
    binding = materialize_component_default_binding(
        role=role, persona=persona, component_root=root,
        dependency_digests=dependency_digests, effective_config_digest=effective_config_digest,
    )
    # compile_prompt_bundle both VALIDATES (ceilings, persona/role/binding
    # cross-consistency) and produces the bundle materialize_operational_files'
    # sibling agent_loader wiring (a later slice) will recompile identically
    # at load time — compiling here is a pre-activation GATE, not a cache.
    compile_prompt_bundle(
        role=role, persona=persona, binding=binding,
        platform_frame=(Path(__file__).parent / "defaults" / "personality"
                         / "platform-frame.md").read_text(encoding="utf-8"),
        safety_kernel=(Path(__file__).parent / "defaults" / "personality"
                       / "safety-kernel.md").read_text(encoding="utf-8"),
    )

    instance_dir.stage_desired(InstanceTuple(
        root=root, binding=binding, config_snapshot=dict(config),
        config_digest=effective_config_digest,
    ))
    # Round-4 fix (finding #2): commit FIRST — the tuple is the single
    # authoritative record (see this function's docstring) — then
    # materialize the operational files as a best-effort derived-cache
    # write. A materialize failure here does NOT roll back the commit; it
    # is caught, logged, and surfaced as a non-fatal last_activation_error.
    # `specialist_materialize.current_specialist_roles_dir` re-materializes
    # every ACTIVE slug's operational files from its tuple on every
    # subsequent boot/reload call, so this slug self-heals automatically.
    committed = instance_dir.commit_desired_to_active()
    last_activation_error: str | None = None
    try:
        specialist_materialize.materialize_specialist_operational_files(
            agents_specialists_dir=agents_specialists_dir, slug=inspection.slug, role=role, persona=persona,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "specialist install %r: operational-file materialize failed post-commit "
            "(%s); will self-heal on next reconcile", inspection.slug, exc, exc_info=True)
        last_activation_error = f"operational files pending reconcile: {exc}"

    return SpecialistInstance(
        slug=inspection.slug, stable_agent_id=f"specialist:{inspection.slug}",
        state="active", active=committed, desired=None, last_activation_error=last_activation_error,
    )


def activate_binding_for_config(cfg, *, specialists_root: Path = Path("/config/specialists")) -> None:
    """Mutates *cfg* in place with the compiled binding for its installed
    component, if one has an ACTIVE tuple (spec §4.1: a pending-configuration
    or legacy-bundled specialist has none, and this is a no-op — cfg keeps
    compiled_prompt_bundle=None, and tools.py's system_prompt seam falls back
    to the legacy cfg.system_prompt path). This is the seam Plan 1 Task 9's
    speaker_provenance_for_role docstring names — 'once Plan 2's N1 populates
    cfg.speaker_provenance for specialists — no further code change needed
    there.'

    Testability note (Task N1b Step 19): rather than inlining this the way
    agent_loader.py's resident block does (which hard-codes
    Path("/config/bindings")), this is a standalone function parameterized
    by ``specialists_root`` — agent_loader.py's specialist branch calls it
    with the real, hard-coded production root; unit tests call it directly
    with a tmp_path root, no monkeypatching needed."""
    from personality_binding import InstanceDir
    from persona_pack import load_persona_pack
    from prompt_compiler import compile_prompt_bundle
    from personality_types import SpeakerProvenance

    instance_dir = InstanceDir(specialists_root / cfg.role_slot.slot)
    active_tuple = instance_dir.active()
    if active_tuple is None:
        return
    # `root` is ALWAYS the component root regardless of binding.mode (Round-2
    # fix, finding #4) — apply_persona_override never rewrites it for a
    # specialist target, so this parse is unconditionally safe.
    _, _, checksum = parse_component_root(active_tuple.root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_root / "store")
    if active_tuple.binding.mode == "override":
        # The persona to COMPILE with is the override's, not the component's
        # bundled default — role/doctrine still come from cas_dir above.
        personas_root = Path("/config/personas")
        bound_persona = load_persona_pack(
            personas_root / active_tuple.binding.persona_id / active_tuple.binding.persona_version / "pack",
            personas_root / active_tuple.binding.persona_id / active_tuple.binding.persona_version / "manifest.json",
        )
    else:
        bound_persona = load_persona_pack(
            cas_dir / "persona" / "pack", cas_dir / "persona" / "manifest.json")
    defaults_root = Path(__file__).parent / "defaults"
    bundle = compile_prompt_bundle(
        role=cfg.role_slot, persona=bound_persona, binding=active_tuple.binding,
        platform_frame=(defaults_root / "personality" / "platform-frame.md").read_text(
            encoding="utf-8"),
        safety_kernel=(defaults_root / "personality" / "safety-kernel.md").read_text(
            encoding="utf-8"),
    )
    cfg.persona_pack = bound_persona
    cfg.binding = active_tuple.binding
    cfg.compiled_prompt_bundle = bundle
    cfg.binding_digest = active_tuple.binding.binding_digest
    cfg.speaker_provenance = SpeakerProvenance(
        speaker_kind="specialist", role_id=cfg.role_slot.role_id,
        persona_id=bound_persona.persona_id, persona_version=bound_persona.version,
        display_name=bound_persona.identity["display_name"],
        binding_digest=active_tuple.binding.binding_digest,
    )


def upgrade_specialist(
    *, slug: str, inspection: "InspectionResult", config: "Mapping[str, str]",
    secret_names_provided: frozenset[str], acks: "SpecialistInstallAckStore",
    specialists_dir: Path = Path("/config/specialists"),
    agents_specialists_dir: Path = Path("/config/agents/specialists"),
) -> "SpecialistInstance":
    """Spec §2.4/§4.1's transactional reinstall/upgrade: stage the new
    version as desired, validate+compile it fully BEFORE touching active,
    commit atomically on success. On ANY failure BEFORE that commit the
    active tuple is left completely untouched — this function never calls
    anything that mutates active.yaml except InstanceDir.commit_desired_to_active
    itself, and that is reached only after every validation gate below has
    already passed.

    Commit-first ordering (matches commit_specialist_install): once every
    validation gate passes and commit_desired_to_active() runs, that IS the
    success boundary the docstring's "never touches active until success"
    refers to — materializing the operational files afterward is a
    best-effort follow-up, not a second gate (see commit_specialist_install's
    docstring for the full rationale: the tuple is the single authoritative
    record; the operational files are a self-healing derived cache
    current_specialist_roles_dir rebuilds on every boot/reload)."""
    from personality_binding import (
        InstanceDir, InstanceTuple, check_persona_requirements, compute_effective_config_digest,
        materialize_component_default_binding, materialize_override_binding,
    )
    from persona_pack import load_persona_pack
    from prompt_compiler import compile_prompt_bundle
    from role_slot import materialize_role
    from role_artifact import load_role_artifact
    from specialist_lifecycle import SpecialistInstance, satisfy_config
    from specialist_install_consent import install_consent_identity
    import specialist_materialize

    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        component_checksum=inspection.root_digest, slug=inspection.slug)
    if not acks.is_acked(identity):
        raise SpecialistInstallError("consent_missing", "no recorded operator approval for the upgrade")

    instance_dir = InstanceDir(specialists_dir / slug)
    active_before = instance_dir.active()
    if active_before is None:
        raise SpecialistInstallError("no_active_tuple", f"{slug!r} has no active install to upgrade")

    # Same CAS-before-verify TEMP-staging + reload + recompute + compare +
    # os.replace pattern as commit_specialist_install (see that function's
    # comments for the full rationale) — a digest mismatch here must never
    # leave a wrong-digest-named CAS directory behind.
    cas_dir = cas_store_dir(inspection.root_digest, store_root=specialists_dir / "store")
    if not cas_dir.exists():
        staging_root = specialists_dir / "store" / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        cas_staging_dir = staging_root / uuid.uuid4().hex
        shutil.copytree(inspection.staged_dir, cas_staging_dir, dirs_exist_ok=False, symlinks=True)
        for path in cas_staging_dir.rglob("*"):
            if path.is_file():
                path.chmod(0o400)
        try:
            staged_component = load_specialist_component(
                cas_staging_dir, cas_staging_dir / "manifest.json")
            staged_deps = resolve_dependency_closure(staged_component, cas_staging_dir)
            staged_unavailable = [d for d in staged_deps if not d.available]
            if staged_unavailable:
                detail = "; ".join(
                    f"{d.kind}:{d.identifier}: {d.detail}" for d in staged_unavailable)
                raise SpecialistInstallError("dependency_unavailable", detail)
            staged_root_digest = compute_install_root_digest(
                staged_component, staged_deps,
                manifest_bytes=(cas_staging_dir / "manifest.json").read_bytes())
            if staged_root_digest != inspection.root_digest:
                raise SpecialistInstallError(
                    "checksum_changed",
                    "staged component no longer matches the approved inspection")
        except Exception:
            shutil.rmtree(cas_staging_dir, ignore_errors=True)
            raise
        os.replace(cas_staging_dir, cas_dir)
    component = load_specialist_component(cas_dir, cas_dir / "manifest.json")
    # The MCP tool boundary passes `slug` and `inspection` as INDEPENDENT
    # arguments — specialist_upgrade builds `inspection` from the freshly-
    # loaded staged component but takes `args["slug"]` separately, so
    # nothing previously stopped a caller (compromised or mistaken tool-call
    # sequence, or a test/direct caller that hand-builds InspectionResult)
    # from upgrading slug X using component Y's bytes. Assert agreement at
    # the lifecycle-function level — the layer every caller, sanctioned or
    # not, must pass through.
    if component.slug != slug:
        raise SpecialistInstallError(
            "slug_mismatch",
            f"component slug {component.slug!r} does not match the requested upgrade slug {slug!r}")
    fresh_deps = resolve_dependency_closure(component, cas_dir)
    fresh_unavailable = [d for d in fresh_deps if not d.available]
    if fresh_unavailable:
        detail = "; ".join(f"{d.kind}:{d.identifier}: {d.detail}" for d in fresh_unavailable)
        raise SpecialistInstallError("dependency_unavailable", detail)
    fresh_root_digest = compute_install_root_digest(
        component, fresh_deps, manifest_bytes=(cas_dir / "manifest.json").read_bytes())
    if fresh_root_digest != inspection.root_digest:
        raise SpecialistInstallError(
            "checksum_changed", "CAS-persisted component no longer matches the approved inspection")
    role = materialize_role(source=load_role_artifact(cas_dir / "role"), options={})
    # An existing OVERRIDE binding must survive an upgrade — the component's
    # own bundled default persona is only used when the active binding was
    # already component-default (or this is a first activation). Reverting
    # silently on every upgrade would discard an operator's explicit
    # persona choice.
    if active_before.binding.mode == "override":
        personas_root = Path("/config/personas")
        persona = load_persona_pack(
            personas_root / active_before.binding.persona_id / active_before.binding.persona_version / "pack",
            personas_root / active_before.binding.persona_id / active_before.binding.persona_version / "manifest.json",
        )
    else:
        persona = load_persona_pack(cas_dir / "persona" / "pack", cas_dir / "persona" / "manifest.json")

    # Re-validate the OPERATOR'S EXISTING non-secret config against the NEW
    # schema, fail-closed, into the DESIRED snapshot only (spec §4.1) — the
    # active config_snapshot is never read or touched here. Keys the NEW
    # schema no longer declares are DROPPED, not carried forward forever —
    # "re-validate ... fail-closed" means the schema is authoritative on
    # every upgrade, not just at fresh install.
    known_keys = set(component.config_schema.get("required", [])) | set(
        component.config_schema.get("secret_names", []))
    stale_config = {**dict(active_before.config_snapshot), **dict(config)}
    dropped_keys = sorted(k for k in stale_config if k not in known_keys)
    merged_config = {k: v for k, v in stale_config.items() if k in known_keys}
    satisfied, missing = satisfy_config(
        schema=component.config_schema, provided_non_secret=merged_config,
        provided_secret_names=secret_names_provided,
    )
    root = component_root_string(component_id=component.component_id, version=component.version,
                                  component_checksum=fresh_root_digest)
    dependency_digests = tuple(sorted(d.digest for d in fresh_deps))

    def _build_upgrade_binding(*, effective_config_digest: str):
        # Reuse the SAME override-vs-default branch the "satisfied" path
        # below needs, so a pending-configuration placeholder ALSO
        # preserves an active override rather than silently dropping it if
        # the operator has to supply missing config later.
        if active_before.binding.mode == "override":
            return materialize_override_binding(
                role=role, persona=persona, override_source=active_before.binding.override_source,
                dependency_digests=dependency_digests, effective_config_digest=effective_config_digest)
        return materialize_component_default_binding(
            role=role, persona=persona, component_root=root,
            dependency_digests=dependency_digests, effective_config_digest=effective_config_digest)

    if not satisfied:
        placeholder = _build_upgrade_binding(
            effective_config_digest=active_before.binding.effective_config_digest)
        instance_dir.stage_desired(InstanceTuple(
            root=root, binding=placeholder, config_snapshot=merged_config,
            config_digest=placeholder.effective_config_digest))
        note = f"missing required config/secret: {missing}"
        if dropped_keys:
            note += f"; dropped_config_keys={dropped_keys}"
        return SpecialistInstance(
            slug=slug, stable_agent_id=f"specialist:{slug}", state="pending-configuration",
            active=active_before, desired=instance_dir.desired(),
            last_activation_error=note)

    try:
        check_persona_requirements(role.normalized, persona)
        effective_config_digest = compute_effective_config_digest(merged_config)
        binding = _build_upgrade_binding(effective_config_digest=effective_config_digest)
        compile_prompt_bundle(
            role=role, persona=persona, binding=binding,
            platform_frame=(Path(__file__).parent / "defaults" / "personality"
                             / "platform-frame.md").read_text(encoding="utf-8"),
            safety_kernel=(Path(__file__).parent / "defaults" / "personality"
                           / "safety-kernel.md").read_text(encoding="utf-8"))
    except ValueError as exc:
        instance_dir.stage_desired(InstanceTuple(
            root=root, binding=active_before.binding, config_snapshot=merged_config,
            config_digest=active_before.binding.effective_config_digest))
        instance_dir.discard_desired(reason=str(exc))
        return SpecialistInstance(
            slug=slug, stable_agent_id=f"specialist:{slug}", state="error",
            active=active_before, desired=None, last_activation_error=str(exc))

    instance_dir.stage_desired(InstanceTuple(
        root=root, binding=binding, config_snapshot=merged_config, config_digest=effective_config_digest))
    # Commit FIRST (every gate above — persona/role compatibility,
    # compile_prompt_bundle — already passed, so this is the authoritative
    # record), THEN materialize as a best-effort follow-up that self-heals
    # via current_specialist_roles_dir if it fails. See
    # commit_specialist_install's docstring for the full rationale.
    committed = instance_dir.commit_desired_to_active()  # new binding digest -> new session epoch
    note = f"dropped_config_keys={dropped_keys}" if dropped_keys else None
    try:
        specialist_materialize.materialize_specialist_operational_files(
            agents_specialists_dir=agents_specialists_dir, slug=slug, role=role, persona=persona)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "specialist upgrade %r: operational-file materialize failed post-commit "
            "(%s); will self-heal on next reconcile", slug, exc, exc_info=True)
        heal_note = f"operational files pending reconcile: {exc}"
        note = f"{note}; {heal_note}" if note else heal_note
    return SpecialistInstance(
        slug=slug, stable_agent_id=f"specialist:{slug}", state="active", active=committed,
        desired=None, last_activation_error=note)


def rollback_specialist(
    *, slug: str, specialists_dir: Path = Path("/config/specialists"),
    agents_specialists_dir: Path = Path("/config/agents/specialists"),
) -> "SpecialistInstance":
    """Restore the RETAINED active.prior.yaml as the new active tuple (spec
    §2.4's rollback target — the prior binding's blobs stay pinned exactly
    because a retained tuple still references them, see Task N1d's
    cas_pin_roots). Rollback IS an upgrade to the prior tuple — reuse
    InstanceDir's own stage/commit, never a bespoke restore path."""
    from personality_binding import InstanceDir, load_instance_tuple
    from role_slot import materialize_role
    from role_artifact import load_role_artifact
    from persona_pack import load_persona_pack
    from specialist_lifecycle import SpecialistInstance
    import specialist_materialize

    instance_dir = InstanceDir(specialists_dir / slug)
    prior_path = specialists_dir / slug / "active.prior.yaml"
    prior = load_instance_tuple(prior_path)
    if prior is None:
        raise SpecialistInstallError("no_prior_tuple", f"{slug!r} has no retained prior tuple")

    # `prior.root` is ALWAYS the component root (the override fix never
    # touches `root`), independent of prior.binding.mode.
    _, _, checksum = parse_component_root(prior.root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_dir / "store")
    role = materialize_role(source=load_role_artifact(cas_dir / "role"), options={})
    if prior.binding.mode == "override":
        personas_root = Path("/config/personas")
        persona = load_persona_pack(
            personas_root / prior.binding.persona_id / prior.binding.persona_version / "pack",
            personas_root / prior.binding.persona_id / prior.binding.persona_version / "manifest.json",
        )
    else:
        persona = load_persona_pack(cas_dir / "persona" / "pack", cas_dir / "persona" / "manifest.json")

    instance_dir.stage_desired(prior)
    # Commit FIRST, same reordering as commit_specialist_install/
    # upgrade_specialist — `prior` is a previously-active, already-validated
    # tuple (it was active.yaml once before), so committing it back is
    # itself the authoritative act; materialize is a best-effort follow-up
    # that self-heals via current_specialist_roles_dir if it fails.
    committed = instance_dir.commit_desired_to_active()
    last_activation_error: str | None = None
    try:
        specialist_materialize.materialize_specialist_operational_files(
            agents_specialists_dir=agents_specialists_dir, slug=slug, role=role, persona=persona)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "specialist rollback %r: operational-file materialize failed post-commit "
            "(%s); will self-heal on next reconcile", slug, exc, exc_info=True)
        last_activation_error = f"operational files pending reconcile: {exc}"

    return SpecialistInstance(
        slug=slug, stable_agent_id=f"specialist:{slug}", state="active", active=committed,
        desired=None, last_activation_error=last_activation_error)


def uninstall_specialist(
    *, slug: str, specialists_dir: Path = Path("/config/specialists"),
    agents_specialists_dir: Path = Path("/config/agents/specialists"),
) -> None:
    """Removes the instance and its legacy operational directory. Does NOT
    delete CAS blobs (Task N1d's GC-root policy: a blob stays pinned while
    ANY tuple of ANY installed specialist references it — deletion here
    would need a full cross-specialist reference scan, which is exactly what
    Task N1d's `cas_pin_roots` builds; the GC SWEEP itself stays deferred
    per this plan's Global Constraints).

    `agents_specialists_dir / slug` is a SYMLINK to a versioned content
    directory (materialize_specialist_operational_files) once the specialist
    has ever materialized, not a real directory. `shutil.rmtree` deliberately
    REFUSES to operate on a symlink (raises OSError; with ignore_errors=True
    it silently does nothing at all) — calling it directly on a symlinked
    slug_dir would leave BOTH the symlink and its target behind, a silent
    uninstall no-op. Unlink the symlink itself, then remove the versioned
    content directory it pointed at."""
    op_dir = agents_specialists_dir / slug
    if op_dir.is_symlink():
        content_dir = agents_specialists_dir / os.readlink(op_dir)
        op_dir.unlink(missing_ok=True)
        shutil.rmtree(content_dir, ignore_errors=True)
    else:
        shutil.rmtree(op_dir, ignore_errors=True)  # legacy real-dir layout, never migrated
    shutil.rmtree(specialists_dir / slug, ignore_errors=True)
