from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from canonical_bytes import reject_forbidden_markers, to_plain_json
from specialist_component import SpecialistComponent, load_specialist_component
from specialist_lifecycle import check_slug_uniqueness

if TYPE_CHECKING:
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
