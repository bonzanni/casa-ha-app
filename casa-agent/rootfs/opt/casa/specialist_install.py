from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping

from authored_markers import contains_forbidden_marker
from canonical_bytes import reject_forbidden_markers, to_plain_json
from specialist_component import SpecialistComponent, is_valid_slug, load_specialist_component
from specialist_lifecycle import check_slug_uniqueness

if TYPE_CHECKING:
    from specialist_install_consent import SpecialistInstallAckStore
    from specialist_lifecycle import SpecialistInstance
    from specialist_receipt import PluginReceiptRow
    from specialist_registry import InstalledSpecialistIndex

logger = logging.getLogger(__name__)


class SpecialistInstallError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.detail = message
        super().__init__(message)


# Whole-branch review F1 (slug traversal) / F4 (corpus identifier containment):
# every lifecycle function that turns a CALLER-SUPPLIED slug or a
# schema-unconstrained dependency identifier into a `Path` join must first
# validate it against a canonical, single-segment shape — a value like
# `../../..`, `/data`, or `a/b` must NEVER index shutil.rmtree/copytree or a
# CAS/corpus lookup. These validators are the ONE authority every entry point
# routes through (fail-closed, typed refusal).
_CORPUS_IDENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def validate_specialist_slug(slug: object) -> str:
    """F1: fail-closed slug gate at the lifecycle-function boundary — the layer
    every caller (MCP tool, test, direct) must pass through. Reuses the
    component loader's canonical slug regex (``specialist_component.
    is_valid_slug`` / role.v1.json ``slot``). Raises a typed
    ``invalid_slug`` error; never lets a traversal/absolute/separator slug
    reach a ``Path`` join."""
    if not is_valid_slug(slug):
        raise SpecialistInstallError("invalid_slug", f"invalid specialist slug {slug!r}")
    return slug  # type: ignore[return-value]


def is_safe_corpus_identifier(identifier: object) -> bool:
    """F4: a corpus dependency's ``identifier`` is schema-unconstrained
    (specialist-component.v1.json only requires ``minLength: 1``) yet it is
    joined as ``component_dir / "corpus" / identifier``. Require a
    conservative single-segment name (no separators, no ``..``, not absolute)
    so a hostile manifest can never make the join stat/hash bytes outside the
    component directory."""
    return (
        isinstance(identifier, str)
        and _CORPUS_IDENT_RE.fullmatch(identifier) is not None
        and ".." not in identifier
    )


# Whole-branch review round 4, F1/F2 — in-lock concurrent-mutation guards.
# EVERY InstanceDir write (stage_desired/commit_desired_to_active/
# discard_desired) in this module now happens under
# specialist_materialize.MATERIALIZE_LOCK (round 3 covered only the activating
# stage+commit; round 4 extends it to the pending-configuration placeholder and
# the upgrade error path). But holding the lock is not enough on its own: a
# pre-lock read (upgrade/rollback's `active_before`, or a fresh install's "the
# slug is not yet active" premise) can go STALE while the caller blocks on the
# lock, because a concurrent uninstall/install/upgrade for the SAME slug may
# have run to completion first. These two helpers RE-VALIDATE that premise
# INSIDE the lock, immediately before the first InstanceDir write, and fail
# closed with a typed `concurrent_mutation` refusal — leaving every on-disk
# tuple untouched — rather than resurrect a removed slug's InstanceDir or
# double-activate over a concurrent winner. Both MUST be called with
# MATERIALIZE_LOCK held.


def _require_active_unchanged(instance_dir, active_before, *, slug: str) -> None:
    """F2: upgrade/rollback captured `active_before` BEFORE taking the lock. A
    concurrent uninstall (which removes specialists/<slug>, active.yaml
    included) or a concurrent upgrade/rollback/persona-override (which commits a
    different active) may have won while we blocked on the lock. Require the
    active tuple to still EXIST and be BYTE-FOR-BYTE the tuple captured at
    `active_before`; a vanished or in-ANY-way-changed active means a concurrent
    mutation won — refuse, so we never stage/commit over it (or recreate a
    just-removed InstanceDir).

    Round-5 fix (F1): compare the FULL tuple (`re_read != active_before`), not
    just `root`. `root` alone (component_id@version#checksum) misses a
    concurrent SAME-ROOT mutation — a config-only upgrade (different
    config_digest/binding on an unchanged component version) or a persona
    override (mode=override, root unchanged) commits a genuinely different
    active tuple that a root-only check waves through, letting this caller
    silently overwrite it with the stale `active_before`. Full-tuple equality
    is the SAME convention InstanceDir.commit_desired_to_active already uses for
    its crash-retry short-circuit (`current_active == candidate`, root
    included); both sides here are round-tripped through the same
    load_instance_tuple/verify_instance_tuple path, so the comparison is fair
    and symmetric (frozen-dataclass value equality over root+binding+
    config_snapshot+config_digest)."""
    current = instance_dir.active()
    if current is None or current != active_before:
        raise SpecialistInstallError(
            "concurrent_mutation",
            f"{slug!r}: the active install changed under a concurrent mutation "
            f"(uninstall/upgrade/rollback/persona-override) while acquiring the lock; "
            f"refusing to overwrite it — retry the operation")


def _refuse_if_active_present(instance_dir, *, slug: str) -> None:
    """F2: a fresh `commit_specialist_install` (both the activating and the
    pending-configuration placeholder paths) presumes the slug is NOT yet
    active — inspect's `check_slug_uniqueness` rejected an already-installed
    slug, and a reinstall-after-uninstall legitimately sees no active tuple
    (uninstall removed it). But two concurrent fresh installs of the SAME slug
    can both clear inspect and then race here; the first commits active, and
    without this guard the second would stage its desired and
    `commit_desired_to_active` would rotate the winner's active into prior and
    write ours over it — a silent double-activate demoting the winner. Re-read
    under the lock and refuse if an active tuple already exists (fail closed),
    rather than clobber a concurrent winner."""
    if instance_dir.active() is not None:
        raise SpecialistInstallError(
            "concurrent_mutation",
            f"{slug!r}: an active install appeared under a concurrent install "
            f"while acquiring the lock; refusing to double-activate — re-inspect "
            f"and retry")


@dataclass(frozen=True, slots=True)
class DependencyResolution:
    kind: str
    identifier: str
    digest: str
    available: bool
    detail: str
    # Task 8 fix-round-1 (consent-review CRITICAL): the sourced-plugin
    # surfaces `_validate_sourced_plugin_tree` already parses while
    # validating — captured here (rather than re-parsed at the
    # PluginReceiptRow-building site) so the consent DM can enumerate them
    # (spec §3.2). Empty for every non-sourced-plugin row (persona/corpus/
    # legacy sourceless plugin) and for a sourced row that failed validation
    # before reaching the point these are extracted.
    mcp_servers: tuple[str, ...] = ()
    protected_tools: tuple[str, ...] = ()
    env_names: tuple[str, ...] = ()


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
    # Task 8 additions (spec §3.2.1) — defaulted so every pre-Task-8
    # constructor call (production and test) keeps working unchanged.
    # ``receipt_id``/``receipt_digest`` are "" and ``plugin_resolutions`` is
    # () for a hand-built InspectionResult that predates the trusted-source
    # receipt; a real inspect_specialist_repo call always populates all three
    # (every inspect issues a receipt, plugin-less components included).
    receipt_id: str = ""
    receipt_digest: str = ""
    plugin_resolutions: tuple["PluginReceiptRow", ...] = ()


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
            # Whole-branch review F3 (persona identity binding): a matching
            # checksum alone proves the bundled bytes are INTERNALLY
            # consistent, NOT that they are the persona the operator
            # approved. Require, in addition to the checksum, that the
            # bundled pack's own identity (`persona_id@version`) IS the
            # declared dependency identifier AND that it matches the
            # manifest's `default_persona` ref+checksum — otherwise a
            # component could ship persona Y under a dependency line naming
            # persona X and slip the substitution past consent. Any mismatch
            # flows into `dependency_unavailable` at both inspect and the
            # commit-time re-verification.
            pack_ref = f"{pack.persona_id}@{pack.version}"
            checksum_ok = pack.checksum == dep.digest
            identity_ok = pack_ref == dep.identifier
            default_ok = (
                component.default_persona_ref == dep.identifier
                and component.default_persona_checksum == dep.digest
            )
            available = checksum_ok and identity_ok and default_ok
            if available:
                detail = ""
            elif not identity_ok:
                detail = (f"bundled persona is {pack_ref!r}, not the declared "
                          f"dependency {dep.identifier!r}")
            elif not checksum_ok:
                detail = "bundled persona checksum does not match manifest"
            else:
                detail = (
                    "bundled persona does not match the component's declared default_persona "
                    f"({component.default_persona_ref}#{component.default_persona_checksum})")
            out.append(DependencyResolution(
                kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                available=available, detail=detail))
        elif dep.kind == "corpus/data":
            # F4: reject a hostile/unsafe corpus identifier BEFORE it ever
            # indexes a filesystem join (`component_dir / "corpus" /
            # identifier`) — nothing outside component_dir is stat'd/hashed.
            if not is_safe_corpus_identifier(dep.identifier):
                out.append(DependencyResolution(
                    kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                    available=False, detail="unsafe corpus identifier"))
                continue
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
            if dep.source is not None:
                # Task 8 (spec §1/§3.2.1): a SOURCED dep resolves its tree from
                # component_dir itself — bundled -> the manifest-declared
                # subtree, github -> the ".dep-plugins" convention every
                # closure call site (inspect/CAS-staging/final-CAS/rollback)
                # shares unconditionally (see inspect_specialist_repo's
                # github-fetch loop) — instead of an already-installed
                # registry plugin (the sourceless/legacy branch below).
                if dep.source.type == "bundled":
                    tree = component_dir / dep.source.path
                else:
                    tree = component_dir / ".dep-plugins" / dep.identifier
                if not tree.is_dir():
                    out.append(DependencyResolution(
                        kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                        available=False, detail="sourced plugin tree missing"))
                    continue
                if dep.source.type == "bundled":
                    # Containment: specialist_component's loader already
                    # rejects a non-canonical (absolute/traversal) path
                    # string, but a symlinked component_dir (or a path
                    # component that is itself a symlink) could still let the
                    # RESOLVED tree escape — assert the resolved location
                    # stays inside the resolved component_dir before this
                    # tree is ever read/hashed.
                    try:
                        tree.resolve().relative_to(component_dir.resolve())
                    except ValueError:
                        out.append(DependencyResolution(
                            kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                            available=False,
                            detail="bundled plugin path escapes the component"))
                        continue
                detail, surfaces = _validate_sourced_plugin_tree(
                    tree, slug=component.slug, identifier=dep.identifier)
                if detail:
                    out.append(DependencyResolution(
                        kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                        available=False, detail=detail))
                    continue
                digest = "sha256:" + content_checksum(tree)
                out.append(DependencyResolution(
                    kind=dep.kind, identifier=dep.identifier, digest=dep.digest,
                    available=(digest == dep.digest),
                    detail="" if digest == dep.digest else
                           "sourced plugin content does not match the pinned digest",
                    mcp_servers=surfaces.mcp_servers,
                    protected_tools=surfaces.protected_tools,
                    env_names=surfaces.env_names))
                continue
            # Legacy/sourceless (spec §1 "no source -> legacy behavior"):
            # UNCHANGED — the dependency must already be plugin_add-installed.
            #
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

    # Whole-branch review F2 (round 2, persona dependency row required
    # STRUCTURALLY): the per-dependency persona LEG above only fires when a
    # persona row is PRESENT. A manifest with `dependencies: []` (or with no
    # kind=="persona" row) would otherwise activate its bundled default persona
    # with NO identity/checksum binding at all — the exact substitution the
    # persona-identity check (F3, Plan-1) exists to prevent, bypassed by simple
    # absence. The component-layout contract requires EXACTLY ONE kind=="persona"
    # dependency whose identifier/digest match the manifest's `default_persona`
    # 1:1. The COUNT invariant (exactly one) is the part the per-row loop cannot
    # see — for a single persona row, the loop already fails-closed on any
    # identity/checksum/default_persona mismatch (its `available` requires
    # identity_ok and default_ok), so appending here ONLY for count != 1 avoids
    # a duplicate resolution while still refusing absence (0 rows) and ambiguity
    # (2+ rows). Enforced in the single closure `inspect_specialist_repo` and
    # `commit_specialist_install` both route through, so it flows into
    # `dependency_unavailable` at BOTH inspect and commit.
    persona_deps = [d for d in component.dependencies if d.kind == "persona"]
    if len(persona_deps) != 1:
        out.append(DependencyResolution(
            kind="persona", identifier=component.default_persona_ref,
            digest=component.default_persona_checksum, available=False,
            detail="persona dependency row missing/mismatched"))
    return tuple(out)


# Task 8 (spec §1, §3.2.1) — prohibition/error-code prefixes a sourced
# plugin's validation detail can start with. `inspect_specialist_repo` scans
# the closure's unavailable rows for these prefixes and raises the matching
# SpecialistInstallError kind INSTEAD OF the generic `dependency_unavailable`
# (Sol plan-r1: a bundle transaction must surface WHY a sourced dep was
# refused, not fold every refusal into one undifferentiated code).
BUNDLED_SYSREQS_UNSUPPORTED = "bundled_sysreqs_unsupported"
BUNDLED_TRIGGERS_UNSUPPORTED = "bundled_triggers_unsupported"
ENV_NAME_COLLISION = "env_name_collision"
_PROHIBITION_KIND_PREFIXES = (
    BUNDLED_SYSREQS_UNSUPPORTED, BUNDLED_TRIGGERS_UNSUPPORTED, ENV_NAME_COLLISION,
)


def _env_name_conflicts(tree_env_names: "set[str]", *, exclude_owner: str) -> "list[str]":
    """Global env-name collision check (spec §1 "Global-namespace collision
    preflight"; brief Step 7). `tree_env_names` are the `${VAR}` references a
    sourced plugin's OWN `.mcp.json` requires (plugin_env_extractor —
    ownership-blind, NOT plugin_env_conf, which stores VALUES not per-plugin
    ownership). The installed-side inventory is the same extraction run over
    every OTHER validated installed artifact's `.mcp.json`
    (`plugin_registry.resolve_all().plugins`), excluding entries owned by
    `exclude_owner` (the slug's own set, being replaced on upgrade — its own
    prior env names are not a collision with its own new ones).

    A monkeypatchable module-level seam by design (Step 3 of the Task 8
    brief) — tests stub this directly rather than building a real registry +
    store fixture to exercise the collision-kind wiring."""
    import plugin_env_extractor
    import plugin_registry

    owned_names = {
        e["name"] for e in plugin_registry.owned_entries_for(
            exclude_owner, plugin_registry.snapshot_registry())
    }
    installed_names: "set[str]" = set()
    for rp in plugin_registry.resolve_all().plugins:
        if rp.name in owned_names:
            continue
        installed_names |= plugin_env_extractor.extract_env_vars(Path(rp.path) / ".mcp.json")
    return sorted(tree_env_names & installed_names)


def _manifest_name_collisions(component: SpecialistComponent) -> "list[str]":
    """Per-target manifest-name precheck (spec §2.1, brief): a sourced dep's
    `identifier` becomes its OWNED entry's effective runtime name
    (`manifest_name`). If some OTHER already-registered entry targeting this
    same `specialist:<slug>` already resolves to that same effective name,
    committing this install would collide at load time (the registry's own
    per-target uniqueness invariant would then quarantine one of them). Fail
    BEFORE any staging/consent, not after. Entries owned by THIS slug are
    excluded — an upgrade legitimately replaces its own prior owned set with
    identical names."""
    import plugin_registry

    sourced_idents = {
        d.identifier for d in component.dependencies
        if d.kind == "plugin/implementation" and d.source is not None
    }
    if not sourced_idents:
        return []
    target = f"specialist:{component.slug}"
    collisions: list[str] = []
    for entry in plugin_registry.snapshot_registry().entries:
        if target not in entry.get("targets", []):
            continue
        if plugin_registry.entry_owner(entry) == target:
            continue  # this slug's own (prior) owned set — replaced, not a collision
        effective = entry.get("manifest_name") or entry.get("name")
        if effective in sourced_idents:
            collisions.append(effective)
    return collisions


@dataclass(frozen=True, slots=True)
class _PluginSurfaces:
    """Task 8 fix-round-1 (consent-review CRITICAL, spec §3.2): the three
    consent-enumeration surfaces `_validate_sourced_plugin_tree` extracts
    from the manifest/`.mcp.json` it already parses while validating —
    captured once here (into the row `resolve_dependency_closure` builds)
    rather than re-parsed at the PluginReceiptRow-building site in
    `inspect_specialist_repo`. Empty (the default) for a tree that failed
    validation before reaching the extraction point."""
    mcp_servers: tuple[str, ...] = ()
    protected_tools: tuple[str, ...] = ()
    env_names: tuple[str, ...] = ()


_EMPTY_SURFACES = _PluginSurfaces()


_MCP_JSON_VAR_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _walk_reject_markers_in_json(value: object) -> None:
    """Parsed-leaf marker scan (mirrors `authored_markers.
    reject_markers_in_parsed`'s dict/list/str walk) with a `${VAR}` carve-out
    applied PER STRING LEAF before the marker check. See
    `_reject_forbidden_markers_in_json` for why both pieces (parsed-leaf, not
    raw-text; `${VAR}`-stripped, not blanket) are required together."""
    if isinstance(value, str):
        if contains_forbidden_marker(_MCP_JSON_VAR_RE.sub("", value)):
            raise ValueError("template, include, HTML, or delimiter detected")
    elif isinstance(value, dict):
        for key, item in value.items():
            _walk_reject_markers_in_json(key)
            _walk_reject_markers_in_json(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_reject_markers_in_json(item)


def _reject_forbidden_markers_in_json(text: str) -> None:
    """Task 8 fix-round-1 (found while wiring the mcp_servers/protected_tools/
    env_names consent surfaces): a plain `reject_forbidden_markers(text)`
    raw-text scan has TWO false-positive sources on real `plugin.json`/
    `.mcp.json` content, both unconditional:

    1. `${VAR}` env-var interpolation is the UNIVERSAL, legitimate
       `.mcp.json` syntax every real plugin uses — `${CLAUDE_PLUGIN_ROOT}`
       in a `command`/`args` entry, `${MY_SECRET}` in an `env` entry (see
       `tests/test_env_var_extraction.py`, `plugin_env_extractor`'s own
       module docstring) — never a template-injection attempt.
    2. Nested JSON objects routinely end two (or more) closing braces in a
       row — e.g. `{"casa": {"protectedTools": ["x"]}}` or
       `{"env": {"K": "V"}}` — an exact byte-for-byte match for the
       forbidden Jinja-close marker `}}`, on pure JSON structural
       punctuation. `casa.protectedTools`/`casa.systemRequirements`/
       `casa.triggers` are ALL nested one level under `casa`, so this hits
       `plugin.json` itself for precisely the shapes this fix-round exists
       to enumerate. The identical class of false positive already forced a
       raw-scan carve-out for role.yaml's flow-style YAML
       (`_extract_full_line_yaml_comments`); `authored_markers.
       reject_markers_in_parsed` is this codebase's existing canonical fix
       for exactly this — scan the PARSED tree's string leaves, never the
       raw structural bytes.

    Both, together, made this scan reject EVERY real `plugin.json` declaring
    `casa.protectedTools` (etc.) and EVERY real `.mcp.json` a sourced/bundled
    plugin dependency could ever declare, unconditionally, before step 4
    (`validate_manifest`)/6/7 below could ever run — silently making Task
    8's own protectedTools/env-name-collision handling dead code for any
    plugin with a realistic manifest or MCP server config.

    Fix: parse first, walk parsed string leaves only (immune to JSON's own
    `}}`), stripping `${VAR}` per leaf before the marker check (still catches
    a genuine `{{`/`{%`/`!include`/HTML-tag/structural-tag smuggled into any
    string value). Malformed JSON falls back to the original blanket raw-text
    scan (`${VAR}`-stripped) — `plugin_store`'s own parse/verdict gates are
    what actually gate malformed JSON; this is defense in depth over
    whatever text is there, not the primary check."""
    try:
        parsed = json.loads(text)
    except ValueError:
        reject_forbidden_markers(_MCP_JSON_VAR_RE.sub("", text))
        return
    _walk_reject_markers_in_json(parsed)


def _mcp_server_summary(name: str, cfg: "Mapping") -> str:
    """One-line "name: command arg1 arg2…" consent-surface summary (spec
    §3.2) for a single validated `.mcp.json` server entry. A `url`-form
    server (no `command`) summarizes as "name: url <url>" instead — still a
    single line naming exactly what the operator's tap would approve."""
    command = cfg.get("command")
    if isinstance(command, str) and command:
        args = cfg.get("args")
        arg_list = [a for a in args if isinstance(a, str)] if isinstance(args, list) else []
        return f"{name}: " + " ".join([command] + arg_list)
    url = cfg.get("url")
    if isinstance(url, str) and url:
        return f"{name}: url {url}"
    return f"{name}: (unrecognized server config)"


def _validate_sourced_plugin_tree(
    tree: Path, *, slug: str, identifier: str,
) -> "tuple[str, _PluginSurfaces]":
    """Full validation of a sourced (bundled/github) plugin dependency's
    staged tree (spec §1/§3.2.1, brief Step 2). Returns `("", surfaces)` when
    the tree is clean (`surfaces` populated for the consent DM — spec §3.2);
    otherwise `(detail, _EMPTY_SURFACES)` with a non-empty detail string —
    for the four prefixes in `_PROHIBITION_KIND_PREFIXES`,
    `inspect_specialist_repo` raises that exact kind; every other non-empty
    detail flows into the generic `dependency_unavailable`.

    Order (Sol plan-r1 — prohibition codes must never be preempted by
    `validate_manifest`'s own `apt_requirements_rejected`/`triggers_invalid`,
    which are legacy per-plugin refusals, not this bundle's distinct codes):

    1. Normalize FIRST (`plugin_store.strip_bytecode_derivatives`) — the SAME
       normalization `_stage_and_swap` applies at publish, run before any
       digest is computed so receipt attestation covers exactly the bytes
       publish will checksum.
    2. Reject an escaping symlink (`plugin_store._reject_escaping_symlinks`).
    3. Prohibitions on the RAW manifest, read directly (before
       `validate_manifest` gets a chance to raise its OWN, differently-coded,
       refusal for the same underlying key): any `manifest_sysreqs` row ⇒
       `bundled_sysreqs_unsupported`; any `casa.triggers` KEY present (even
       malformed) ⇒ `bundled_triggers_unsupported`.
    4. `plugin_store.validate_manifest` (identity: `plugin.json::name` must
       equal `identifier`; this is also where a non-prohibited
       `apt_requirements_rejected`/`triggers_invalid`/`name_mismatch`/etc.
       surfaces for anything the raw-manifest prohibitions above did not
       already catch — impossible for sysreqs/triggers by construction, but
       real for name mismatches and malformed protectedTools).
    5. Untrusted-bytes marker scan over `plugin.json`, every `*.md`, and every
       `.mcp.json` under the tree (symlinks already vetted in step 2); the
       `plugin.json`/`.mcp.json` scans (fix-round-1) walk PARSED string
       leaves (immune to JSON's own structural `}}`) and strip legitimate
       `${VAR}` env-var interpolation per leaf — see
       `_reject_forbidden_markers_in_json`. `*.md` prose stays a raw scan.
    6. Reserved-env + command-verdict gates over the tree's `.mcp.json`
       (`plugin_store.reserved_env_violations` / `mcp_command_verdicts`).
    7. Env-name collision (`_env_name_conflicts`) against every OTHER
       installed plugin's required env names, excluding this slug's own
       (prior) owned set.
    """
    import plugin_env_extractor
    import plugin_registry
    import plugin_store

    plugin_store.strip_bytecode_derivatives(tree)
    try:
        plugin_store._reject_escaping_symlinks(tree)
    except plugin_store.StoreError as exc:
        return f"{exc.reason_code}: {exc}", _EMPTY_SURFACES

    manifest_path = tree / ".claude-plugin" / "plugin.json"
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"manifest_invalid: plugin.json missing/unparseable: {exc}", _EMPTY_SURFACES
    if not isinstance(raw_manifest, dict):
        return "manifest_invalid: plugin.json is not an object", _EMPTY_SURFACES

    if plugin_store.manifest_sysreqs(raw_manifest):
        return (f"{BUNDLED_SYSREQS_UNSUPPORTED}: a sourced/bundled plugin dependency "
                "must not declare casa.systemRequirements", _EMPTY_SURFACES)
    casa = raw_manifest.get("casa")
    if isinstance(casa, dict) and "triggers" in casa:
        return (f"{BUNDLED_TRIGGERS_UNSUPPORTED}: a sourced/bundled plugin dependency "
                "must not declare casa.triggers", _EMPTY_SURFACES)

    scoped = plugin_registry.scoped_name(slug, identifier)
    try:
        plugin_store.validate_manifest(tree, scoped, manifest_name=identifier)
    except plugin_store.StoreError as exc:
        return f"{exc.reason_code}: {exc}", _EMPTY_SURFACES

    try:
        _reject_forbidden_markers_in_json(manifest_path.read_text(encoding="utf-8"))
        for md_path in sorted(tree.rglob("*.md")):
            reject_forbidden_markers(md_path.read_text(encoding="utf-8"))
        for mcp_path in sorted(tree.rglob(".mcp.json")):
            _reject_forbidden_markers_in_json(mcp_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return f"forbidden_markers: {exc}", _EMPTY_SURFACES
    except OSError as exc:
        return f"forbidden_markers: unreadable file during marker scan: {exc}", _EMPTY_SURFACES

    mcp_json_path = tree / ".mcp.json"
    reserved = plugin_store.reserved_env_violations(mcp_json_path)
    if reserved:
        return "mcp_reserved_env: " + "; ".join(reserved), _EMPTY_SURFACES
    missing = [v for v in plugin_store.mcp_command_verdicts(mcp_json_path, tree)
               if v.get("status") == "missing"]
    if missing:
        return ("mcp_command_missing: " + "; ".join(
            f"{v['server']}:{v['ref']} ({v.get('reason', '')})" for v in missing), _EMPTY_SURFACES)

    tree_env_names = plugin_env_extractor.extract_env_vars(mcp_json_path)
    conflicts = _env_name_conflicts(tree_env_names, exclude_owner=slug)
    if conflicts:
        return f"{ENV_NAME_COLLISION}: colliding env name(s): " + ", ".join(conflicts), _EMPTY_SURFACES

    # Task 8 fix-round-1: the consent-enumeration surfaces (spec §3.2),
    # extracted from state already parsed above — `raw_manifest` (step 3/4)
    # and `mcp_json_path`/`tree_env_names` (step 6/7) — never re-read from
    # disk. Sorted for determinism (dict/set iteration order is not a
    # contract either the manifest author or `extract_env_vars` promises).
    surfaces = _PluginSurfaces(
        mcp_servers=tuple(
            _mcp_server_summary(name, cfg)
            for name, cfg in sorted(plugin_store.mcp_servers_map(mcp_json_path).items())),
        protected_tools=tuple(
            e["name"] for e in plugin_store.manifest_protected_tools(raw_manifest)),
        env_names=tuple(sorted(tree_env_names)),
    )
    return "", surfaces


def _sourced_plugin_manifest_version(tree: Path) -> str:
    """The receipt row's `version` — read AFTER `_validate_sourced_plugin_tree`
    has already fully validated the manifest; mirrors `validate_manifest`'s
    own missing-version default (`"0.0.0"`) without re-running the whole
    validation gate a second time just to extract one field."""
    try:
        manifest = json.loads((tree / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "0.0.0"
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return version if isinstance(version, str) and version else "0.0.0"


def _extract_full_line_yaml_comments(text: str) -> str:
    """Task N2 fix: role.yaml's own legitimate flow-style syntax (e.g.
    `disclosure: {policy: delegated, overrides: {}}`, used by every
    hand-authored role.yaml this repo ships, finance's and mtg's included)
    contains a literal `}}` byte-for-byte identical to the forbidden
    template-close marker — role_artifact.py's own loader deliberately
    never raw-text-scans role.yaml for exactly this reason (see its module
    docstring), relying on the parsed-leaf scan instead. This function
    narrows _validate_untrusted_bytes's raw scan to just the full-line
    comments (a line whose stripped form starts with '#') — the ONE thing
    the parsed-leaf scan structurally cannot see (YAML comments never
    survive parsing) and the ONLY threat model this belt-and-suspenders
    check exists to close (see
    tests/test_specialist_install.py's `_write_component_with_role_yaml_
    comment_marker`) — without re-raw-scanning the structural YAML bytes
    that collide with a forbidden marker by pure syntactic coincidence."""
    return "\n".join(line for line in text.splitlines() if line.strip().startswith("#"))


def _validate_untrusted_bytes(component: SpecialistComponent) -> None:
    """Extra check role_artifact.load_role_artifact does not perform: reject
    templating/HTML/delimiter markers hidden in a YAML COMMENT of the
    FETCHED role.yaml (invisible to the parsed-leaf scan, since comments
    never survive YAML parsing), plus the doctrine.md prose — both
    adversarial input, unlike image-owned role artifacts."""
    import yaml

    role_text = component.role.role_path.read_text(encoding="utf-8")
    try:
        reject_forbidden_markers(_extract_full_line_yaml_comments(role_text))
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
    receipts_dir: Path = Path("/config/specialists/.receipts"),
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
    fresh install past collision checks under a false 'upgrade' claim).

    Task 8 (spec §1/§3.2.1): fetches every github-sourced plugin dependency
    into `.dep-plugins/<identifier>` under the SAME staging dir, runs a
    per-target manifest-name collision precheck, resolves the full
    dependency closure (now including sourced-plugin validation), raises a
    prohibition's OWN kind (bundled_sysreqs_unsupported/
    bundled_triggers_unsupported/env_name_collision) ahead of the generic
    dependency_unavailable, and — for EVERY inspection, plugin-less
    components included — mints and persists a trusted source receipt
    (`specialist_receipt`) so a bundled/declared plugin closure's provenance
    is bound into consent (`receipt_digest`) and available to commit."""
    import plugin_registry
    import specialist_receipt
    from specialist_registry import InstalledSpecialistIndex, _discover_image_role_slots

    if mode == "upgrade" and not target_slug:
        raise SpecialistInstallError("target_slug_required", "mode='upgrade' requires target_slug")
    if target_slug is not None:
        # F1: a caller-supplied target_slug is joined as `specialists_dir /
        # target_slug` below (InstanceDir.active()) — validate before any join.
        validate_specialist_slug(target_slug)

    staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    component_dir = staging_root / uuid.uuid4().hex
    component_revision = resolve_and_fetch(
        repo, ref, subdir, component_dir, expected_revision=expected_revision)

    manifest_path = component_dir / "manifest.json"
    if not manifest_path.is_file():
        raise SpecialistInstallError("manifest_missing", f"{repo}@{ref}: manifest.json not found")
    try:
        component = load_specialist_component(component_dir, manifest_path)
    except ValueError as exc:
        raise SpecialistInstallError("manifest_invalid", str(exc)) from exc

    _validate_untrusted_bytes(component)

    # Task 8: fetch every github-sourced plugin dependency INTO this same
    # staging tree, at the `.dep-plugins/<identifier>` convention every
    # closure call site (inspect/CAS-staging/final-CAS/rollback) shares
    # unconditionally — `dep.identifier` is already PLUGIN_IDENT_RE-validated
    # (specialist_component's loader), so this join is safe without a
    # separate containment check. Revision-pinned: `resolve_and_fetch` itself
    # refuses a moved `ref` against `dep.source.revision`.
    for dep in component.dependencies:
        if dep.kind == "plugin/implementation" and dep.source is not None \
                and dep.source.type == "github":
            dest = component_dir / ".dep-plugins" / dep.identifier
            resolve_and_fetch(dep.source.repo, dep.source.ref, "", dest,
                              expected_revision=dep.source.revision)

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

    manifest_name_collisions = _manifest_name_collisions(component)
    if manifest_name_collisions:
        raise SpecialistInstallError(
            "manifest_name_collision",
            f"specialist:{component.slug} already resolves an entry with effective "
            f"manifest name(s) {sorted(set(manifest_name_collisions))!r} — a sourced "
            "plugin dependency must not collide with it")

    dependencies = resolve_dependency_closure(component, component_dir)
    unavailable = [d for d in dependencies if not d.available]
    if unavailable:
        # Task 8: a prohibition (sysreqs/triggers/env-collision) aborts with
        # its OWN kind, not the generic dependency_unavailable — scan every
        # unavailable row first (Sol plan-r1).
        for d in unavailable:
            for prefix in _PROHIBITION_KIND_PREFIXES:
                if d.detail.startswith(prefix):
                    raise SpecialistInstallError(prefix, f"{d.kind}:{d.identifier}: {d.detail}")
        detail = "; ".join(f"{d.kind}:{d.identifier}: {d.detail}" for d in unavailable)
        raise SpecialistInstallError("dependency_unavailable", detail)

    root_digest = compute_install_root_digest(
        component, dependencies, manifest_bytes=manifest_path.read_bytes())

    # Task 8: build one PluginReceiptRow per sourced plugin dependency
    # (dependencies is 1:1 positional with component.dependencies — every
    # row appended by resolve_dependency_closure's per-dependency loop, and
    # any additional synthetic persona-count row would have been `available=
    # False` and already raised above, so this dict is exhaustive here).
    resolved_by_identity = {(d.kind, d.identifier): d for d in dependencies}
    plugin_rows: list[specialist_receipt.PluginReceiptRow] = []
    for dep in component.dependencies:
        if dep.kind != "plugin/implementation" or dep.source is None:
            continue
        resolution = resolved_by_identity[(dep.kind, dep.identifier)]
        if dep.source.type == "bundled":
            tree = component_dir / dep.source.path
            row_repo, row_ref, row_revision = repo, ref, f"git:{component_revision}"
            row_subdir = f"{subdir}/{dep.source.path}" if subdir else dep.source.path
        else:
            tree = component_dir / ".dep-plugins" / dep.identifier
            row_repo, row_ref, row_revision = dep.source.repo, dep.source.ref, dep.source.revision
            row_subdir = ""
        plugin_rows.append(specialist_receipt.PluginReceiptRow(
            identifier=dep.identifier,
            scoped_name=plugin_registry.scoped_name(component.slug, dep.identifier),
            manifest_name=dep.identifier,
            version=_sourced_plugin_manifest_version(tree),
            source_type=dep.source.type,
            repo=row_repo, ref=row_ref, revision=row_revision, subdir=row_subdir,
            content_digest=resolution.digest, staged_path=str(tree),
            # Task 8 fix-round-1 (consent-review CRITICAL): captured by
            # `_validate_sourced_plugin_tree` during `resolve_dependency_
            # closure` above — never re-parsed here.
            mcp_servers=resolution.mcp_servers,
            protected_tools=resolution.protected_tools,
            env_names=resolution.env_names,
        ))

    receipt = specialist_receipt.build_receipt(
        slug=component.slug, component_repo=repo, component_ref=ref,
        component_revision=f"git:{component_revision}", component_subdir=subdir,
        component_staged_path=str(component_dir), plugins=tuple(plugin_rows),
    )
    specialist_receipt.persist(receipt, receipts_dir=receipts_dir)

    required = component.config_schema.get("required", [])
    secret_names = set(component.config_schema.get("secret_names", []))
    logger.info(
        "inspect_specialist_repo passed all gates: mode=%s slug=%s component_id=%s "
        "version=%s root_digest=%s receipt_id=%s (staged at %s, not yet activated)",
        mode, component.slug, component.component_id, component.version,
        root_digest, receipt.receipt_id, component_dir,
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
        receipt_id=receipt.receipt_id, receipt_digest=receipt.receipt_digest,
        plugin_resolutions=tuple(plugin_rows),
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


def _publish_cas_staging(cas_staging_dir: Path, cas_dir: Path) -> None:
    """Atomically publish a verified staging directory into its final
    content-addressed `cas_dir` via `os.replace`.

    Md (concurrent-install CAS race): commit/upgrade check `cas_dir.exists()`
    and, if absent, stage + verify + publish. Two installs of the SAME digest
    racing can both pass the exists() check; the loser's `os.replace` then
    lands on a now-populated `cas_dir` and raises `OSError` (ENOTEMPTY —
    POSIX rename refuses a non-empty directory target). CAS content is
    immutable/content-addressed, so the winner's bytes are byte-identical to
    ours: discard our staging copy and return, letting the shared
    re-load-and-verify path downstream run against the winner's `cas_dir`.
    Any OTHER failure (or an OSError where `cas_dir` did NOT appear) cleans up
    staging and re-raises — never silently swallowed."""
    try:
        os.replace(cas_staging_dir, cas_dir)
    except OSError:
        if cas_dir.exists():
            shutil.rmtree(cas_staging_dir, ignore_errors=True)
            return
        shutil.rmtree(cas_staging_dir, ignore_errors=True)
        raise


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

    # F1 (round 2, inspection.slug traversal): `inspection.slug` is joined as
    # `specialists_dir / inspection.slug` (InstanceDir) and threaded as the
    # materialize slug below. A hand-built InspectionResult (or a compromised
    # tool layer) with a matching ack could otherwise drive a Path join with a
    # traversal slug. Validate at the lifecycle-function boundary every caller
    # routes through — before consent, before any filesystem write — and
    # re-assert after the post-publish CAS reload that the component's OWN slug
    # agrees (mirroring upgrade_specialist's slug_mismatch treatment).
    validate_specialist_slug(inspection.slug)

    # Task 8 seam (Task 7 P0): thread the trusted-source receipt digest into
    # the consent identity — `getattr` keeps this call working against a
    # hand-built/legacy InspectionResult that predates the field (default "").
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug,
        receipt_digest=getattr(inspection, "receipt_digest", ""),
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
        _publish_cas_staging(cas_staging_dir, cas_dir)

    # Re-load from the now-final (or, for a digest an earlier install
    # already verified and published, pre-existing) CAS directory.
    component = load_specialist_component(cas_dir, cas_dir / "manifest.json")
    # F1 (round 2): the CAS component's OWN declared slug is authoritative —
    # assert it agrees with the approved inspection slug (a mismatch means the
    # inspection was hand-built/tampered to bind slug X's approval to component
    # Y's bytes). Mirrors upgrade_specialist's slug_mismatch guard.
    if component.slug != inspection.slug:
        raise SpecialistInstallError(
            "slug_mismatch",
            f"CAS component slug {component.slug!r} does not match the approved "
            f"inspection slug {inspection.slug!r}")
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
        # F1 (round 4): stage_desired for the placeholder is an InstanceDir
        # write — it MUST hold MATERIALIZE_LOCK like every other one. F2: this
        # stages a desired for a not-yet-active slug; re-check under the lock
        # that a concurrent full install has not activated it meanwhile
        # (staging a pending placeholder over a live active would corrupt state).
        with specialist_materialize.MATERIALIZE_LOCK:
            _refuse_if_active_present(instance_dir, slug=inspection.slug)
            instance_dir.stage_desired(InstanceTuple(
                root=root, binding=placeholder_binding, config_snapshot=dict(config),
                config_digest=placeholder_binding.effective_config_digest,
            ))
        return SpecialistInstance(
            slug=inspection.slug, stable_agent_id=f"specialist:{inspection.slug}",
            state="pending-configuration", active=None, desired=instance_dir.desired(),
            last_activation_error=f"missing required config/secret: {missing}",
        )

    # Mc: check_persona_requirements raises a BARE ValueError on
    # incompatibility. commit_specialist_install has no local ValueError
    # handler, so it would escape as an unstructured MCP error rather than
    # the tool's typed {ok, kind} contract (specialist_install_commit catches
    # only SpecialistInstallError). Wrap it typed.
    try:
        check_persona_requirements(role.normalized, persona)
    except ValueError as exc:
        raise SpecialistInstallError("persona_incompatible", str(exc)) from exc
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

    # Round-4 fix (finding #2): commit FIRST — the tuple is the single
    # authoritative record (see this function's docstring) — then
    # materialize the operational files as a best-effort derived-cache
    # write. A materialize failure here does NOT roll back the commit; it
    # is caught, logged, and surfaced as a non-fatal last_activation_error.
    # `specialist_materialize.current_specialist_roles_dir` re-materializes
    # every ACTIVE slug's operational files from its tuple on every
    # subsequent boot/reload call, so this slug self-heals automatically.
    # F3 (round 2): hold MATERIALIZE_LOCK across commit+materialize so the
    # self-heal reconcile loop (also under this lock) can never snapshot the
    # OLD active tuple and then materialize its op-files over this NEW binding's
    # (new binding paired with old capabilities). The lock spans ONLY this
    # tuple-commit+materialize; every compile/fetch gate above already ran
    # outside it. See specialist_materialize.MATERIALIZE_LOCK's deadlock note.
    # F1 (round 3): stage_desired MUST be inside the lock too — two concurrent
    # mutations on the same slug could otherwise interleave (A stages, B stages
    # over desired.yaml, A commits B's tuple while materializing A's op-files),
    # publishing an active tuple whose op-files/marker belong to a different
    # mutation. Staging+commit+materialize+marker is now one atomic locked unit.
    last_activation_error: str | None = None
    with specialist_materialize.MATERIALIZE_LOCK:
        # F2 (round 4): fail closed if a concurrent install activated this slug
        # while we blocked on the lock — never double-activate over a winner.
        _refuse_if_active_present(instance_dir, slug=inspection.slug)
        instance_dir.stage_desired(InstanceTuple(
            root=root, binding=binding, config_snapshot=dict(config),
            config_digest=effective_config_digest,
        ))
        committed = instance_dir.commit_desired_to_active()
        try:
            specialist_materialize.materialize_specialist_operational_files(
                agents_specialists_dir=agents_specialists_dir, slug=inspection.slug, role=role, persona=persona,
                binding_digest=committed.binding.binding_digest, component_root=committed.root,
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

    # F1 (confirm): cfg.role_slot.slot is already loader-validated
    # (role_artifact enforces role.v1.json's slot pattern), but this function
    # joins it as `specialists_root / slot` — re-assert the canonical shape so
    # the containment guarantee holds regardless of how cfg was constructed.
    validate_specialist_slug(cfg.role_slot.slot)
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

    # F1: `slug` is a caller-supplied argument independent of `inspection`;
    # it indexes `specialists_dir / slug` (and downstream Path joins) below.
    # Validate at the top before any filesystem operation.
    validate_specialist_slug(slug)

    # Task 8 seam (Task 7 P0): see commit_specialist_install's identical note.
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug,
        receipt_digest=getattr(inspection, "receipt_digest", ""))
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
        _publish_cas_staging(cas_staging_dir, cas_dir)
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
        # F1 (round 4): the placeholder stage_desired is an InstanceDir write —
        # hold MATERIALIZE_LOCK. F2: `active_before` was read before the lock;
        # re-read inside it and refuse if a concurrent uninstall removed the
        # slug or a concurrent upgrade changed its root (staging here would else
        # recreate a just-removed InstanceDir).
        with specialist_materialize.MATERIALIZE_LOCK:
            _require_active_unchanged(instance_dir, active_before, slug=slug)
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

    # Mc: a persona↔role incompatibility is a hard, typed refusal — raise
    # SpecialistInstallError("persona_incompatible") BEFORE any desired tuple
    # is staged (active stays untouched, matching upgrade's transactional
    # contract) so the tool surfaces {ok:false, kind:persona_incompatible}
    # rather than the generic error-state below (which stays reserved for a
    # compile/ceiling ValueError, a genuinely different failure class).
    try:
        check_persona_requirements(role.normalized, persona)
    except ValueError as exc:
        raise SpecialistInstallError("persona_incompatible", str(exc)) from exc
    try:
        effective_config_digest = compute_effective_config_digest(merged_config)
        binding = _build_upgrade_binding(effective_config_digest=effective_config_digest)
        compile_prompt_bundle(
            role=role, persona=persona, binding=binding,
            platform_frame=(Path(__file__).parent / "defaults" / "personality"
                             / "platform-frame.md").read_text(encoding="utf-8"),
            safety_kernel=(Path(__file__).parent / "defaults" / "personality"
                           / "safety-kernel.md").read_text(encoding="utf-8"))
    except ValueError as exc:
        # F1 (round 4): this error-path stage_desired + discard_desired are
        # InstanceDir writes — hold MATERIALIZE_LOCK. F2: `active_before` was
        # read before the lock; re-read inside it and refuse if a concurrent
        # uninstall/upgrade won, so recording this compile error never
        # resurrects a just-removed slug's InstanceDir (stage_desired would
        # recreate specialists/<slug>/desired.yaml). The concurrent-mutation
        # refusal supersedes the (now-moot) compile error for a vanished slug.
        with specialist_materialize.MATERIALIZE_LOCK:
            _require_active_unchanged(instance_dir, active_before, slug=slug)
            instance_dir.stage_desired(InstanceTuple(
                root=root, binding=active_before.binding, config_snapshot=merged_config,
                config_digest=active_before.binding.effective_config_digest))
            instance_dir.discard_desired(reason=str(exc))
        return SpecialistInstance(
            slug=slug, stable_agent_id=f"specialist:{slug}", state="error",
            active=active_before, desired=None, last_activation_error=str(exc))

    # Commit FIRST (every gate above — persona/role compatibility,
    # compile_prompt_bundle — already passed, so this is the authoritative
    # record), THEN materialize as a best-effort follow-up that self-heals
    # via current_specialist_roles_dir if it fails. See
    # commit_specialist_install's docstring for the full rationale.
    # F3 (round 2): commit+materialize under MATERIALIZE_LOCK — see
    # commit_specialist_install's F3 note and the lock's deadlock analysis.
    # F1 (round 3): stage_desired is inside the lock so stage+commit+materialize
    # is one atomic unit against a concurrent same-slug mutation — see
    # commit_specialist_install's F1 note.
    note = f"dropped_config_keys={dropped_keys}" if dropped_keys else None
    with specialist_materialize.MATERIALIZE_LOCK:
        # F2 (round 4): `active_before` was read before the lock; re-read inside
        # it and refuse if a concurrent uninstall removed the slug or a
        # concurrent upgrade/rollback committed a different active — never
        # commit this upgrade over a concurrent winner or recreate a removed dir.
        _require_active_unchanged(instance_dir, active_before, slug=slug)
        instance_dir.stage_desired(InstanceTuple(
            root=root, binding=binding, config_snapshot=merged_config, config_digest=effective_config_digest))
        committed = instance_dir.commit_desired_to_active()  # new binding digest -> new session epoch
        try:
            specialist_materialize.materialize_specialist_operational_files(
                agents_specialists_dir=agents_specialists_dir, slug=slug, role=role, persona=persona,
                binding_digest=committed.binding.binding_digest, component_root=committed.root)
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
    from prompt_compiler import compile_prompt_bundle
    from role_slot import materialize_role
    from role_artifact import load_role_artifact
    from persona_pack import load_persona_pack
    from specialist_lifecycle import SpecialistInstance
    import specialist_materialize

    # F1: `slug` is caller-supplied and indexes `specialists_dir / slug`.
    validate_specialist_slug(slug)

    instance_dir = InstanceDir(specialists_dir / slug)
    prior_path = specialists_dir / slug / "active.prior.yaml"
    prior = load_instance_tuple(prior_path)
    if prior is None:
        raise SpecialistInstallError("no_prior_tuple", f"{slug!r} has no retained prior tuple")

    # F2 (round 4): capture the CURRENT active BEFORE the lock (like upgrade's
    # `active_before`) so the in-lock re-read below can detect a concurrent
    # uninstall/upgrade/rollback that ran while we validated + blocked on the
    # lock. Rollback replaces the running active with `prior`; a rollback with
    # no current active would be a resurrection (staging `prior` recreates a
    # just-removed InstanceDir), so require an active to roll back FROM.
    active_before = instance_dir.active()
    if active_before is None:
        raise SpecialistInstallError(
            "no_active_tuple", f"{slug!r} has no active install to roll back")

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

    # Whole-branch review F6 (rollback verification gate): the prior tuple was
    # valid when it was active, but the world may have changed since —
    # a pinned plugin dependency can have been uninstalled/re-published out
    # from under it. Re-run the SAME pre-activation gates upgrade uses (full
    # dependency-closure availability against the prior's CAS bytes + a
    # compile of role/persona/prior-binding) BEFORE staging/committing, so a
    # rollback into a now-broken tuple is refused with a typed error and the
    # current active tuple keeps running untouched.
    prior_component = load_specialist_component(cas_dir, cas_dir / "manifest.json")
    prior_deps = resolve_dependency_closure(prior_component, cas_dir)
    unavailable = [d for d in prior_deps if not d.available]
    if unavailable:
        detail = "; ".join(f"{d.kind}:{d.identifier}: {d.detail}" for d in unavailable)
        raise SpecialistInstallError("dependency_unavailable", detail)
    try:
        compile_prompt_bundle(
            role=role, persona=persona, binding=prior.binding,
            platform_frame=(Path(__file__).parent / "defaults" / "personality"
                             / "platform-frame.md").read_text(encoding="utf-8"),
            safety_kernel=(Path(__file__).parent / "defaults" / "personality"
                           / "safety-kernel.md").read_text(encoding="utf-8"),
        )
    except ValueError as exc:
        raise SpecialistInstallError("compile_failed", str(exc)) from exc

    # Commit FIRST, same reordering as commit_specialist_install/
    # upgrade_specialist — `prior` is a previously-active, already-validated
    # tuple (it was active.yaml once before), so committing it back is
    # itself the authoritative act; materialize is a best-effort follow-up
    # that self-heals via current_specialist_roles_dir if it fails.
    # F3 (round 2): commit+materialize under MATERIALIZE_LOCK — see
    # commit_specialist_install's F3 note and the lock's deadlock analysis.
    # F1 (round 3): stage_desired (of the already-loaded `prior` tuple) is
    # inside the lock so stage+commit+materialize is one atomic unit against a
    # concurrent same-slug mutation — see commit_specialist_install's F1 note.
    last_activation_error: str | None = None
    with specialist_materialize.MATERIALIZE_LOCK:
        # F2 (round 4): re-read active under the lock and refuse if it vanished
        # (concurrent uninstall) or its root changed (concurrent upgrade/rollback)
        # since `active_before` — never roll back over a concurrent winner or
        # resurrect a removed InstanceDir.
        _require_active_unchanged(instance_dir, active_before, slug=slug)
        instance_dir.stage_desired(prior)
        committed = instance_dir.commit_desired_to_active()
        try:
            specialist_materialize.materialize_specialist_operational_files(
                agents_specialists_dir=agents_specialists_dir, slug=slug, role=role, persona=persona,
                binding_digest=committed.binding.binding_digest, component_root=committed.root)
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
    content directory it pointed at.

    F1: `slug` is caller-supplied and indexes both `agents_specialists_dir /
    slug` and `specialists_dir / slug` — validate before either join.
    F2: `os.readlink` on the materialize symlink used to be turned into an
    `shutil.rmtree` target directly. A PRE-EXISTING malicious or accidental
    symlink whose target is absolute (`finance -> /data`) or escapes the
    directory would make that rmtree delete out-of-tree content.
    `resolve_material_content_dir` fails closed on any non-contained target —
    then we unlink ONLY the symlink and never rmtree its target."""
    import specialist_materialize
    from specialist_materialize import resolve_material_content_dir

    validate_specialist_slug(slug)
    # F2 (round 3): hold MATERIALIZE_LOCK across the WHOLE removal — op symlink/
    # content AND the InstanceDir tree. Without it a self-heal reconcile that
    # already passed its in-lock active-tuple re-read could rematerialize this
    # slug's op-dir from retained CAS bytes AFTER uninstall removed it,
    # resurrecting the removed specialist until the next reconcile. Serializing
    # under the same lock makes the two orderings both safe: reconcile-then-
    # uninstall completes the rematerialize, then uninstall removes it; uninstall-
    # then-reconcile removes active.yaml FIRST, so the reconcile's in-lock
    # `InstanceDir(...).active()` re-read (specialist_materialize
    # _reconcile_specialist_operational_files) yields None and the slug is
    # skipped — no resurrection either way. Removing specialists/<slug> (which
    # holds active.yaml) inside the lock is what makes that re-read authoritative.
    with specialist_materialize.MATERIALIZE_LOCK:
        op_dir = agents_specialists_dir / slug
        if op_dir.is_symlink():
            content_dir = resolve_material_content_dir(op_dir, agents_specialists_dir)
            op_dir.unlink(missing_ok=True)
            if content_dir is not None:
                shutil.rmtree(content_dir, ignore_errors=True)
            else:
                logger.warning(
                    "uninstall %r: operational symlink target failed containment; removed the "
                    "symlink only, left its (out-of-tree) target untouched", slug)
        else:
            shutil.rmtree(op_dir, ignore_errors=True)  # legacy real-dir layout, never migrated
        shutil.rmtree(specialists_dir / slug, ignore_errors=True)


# ---------------------------------------------------------------------------
# CAS/persona pin-reference roots (Task N1d, spec §4.4)
# ---------------------------------------------------------------------------


def cas_pin_roots(specialists_dir: Path = Path("/config/specialists")) -> frozenset[str]:
    """Spec §4.4's CAS retention roots — every root_digest referenced by ANY
    installed specialist's active, desired, OR retained-prior tuple. This is
    the pin/reference AUTHORITY; a GC sweep that deletes anything NOT in
    this set is deferred (Global Constraints) but the roots this function
    returns are exactly what such a sweep would need.

    Round-2 fix (finding #8, and a bug the finding #4 override fix exposed):
    parses `tup.root` (the InstanceTuple-level field), NOT
    `tup.binding.component_root` — for an OVERRIDE-mode specialist binding,
    `BindingRecord.component_root` is always `None` (only component-default
    mode populates it; override mode populates `override_source` instead),
    but `InstanceTuple.root` still holds the real component CAS root in
    EVERY mode (finding #4's `apply_persona_override` fix never touches
    `root` for a specialist target) — the original `tup.binding.
    component_root is None: continue` guard would silently un-pin an
    override-applied specialist's component root on every scan, an
    immediate GC-root regression for the exact case this task exists to
    close.

    This function only ever pins SPECIALIST COMPONENT blobs (CAS-addressed
    under `specialists_dir/store/`) — there is nothing image-bundled to pin
    here, since a specialist has no "image-default" binding mode (only
    residents do; see `personality_binding.py`'s mode enum). Spec §4.4's
    "current image defaults are pinned" requirement is closed on the
    PERSONA side instead, by `persona_pin_roots` below, which every caller
    of this function should call alongside it to get the complete pin-root
    set."""
    from personality_binding import load_instance_tuple

    pinned: set[str] = set()
    if not specialists_dir.is_dir():
        return frozenset(pinned)
    for entry in sorted(specialists_dir.iterdir()):
        if not entry.is_dir() or entry.name in {"store", ".staging", ".roles-overlay"}:
            continue
        for filename in ("active.yaml", "desired.yaml", "active.prior.yaml"):
            path = entry / filename
            if not path.is_file():
                continue
            tup = load_instance_tuple(path)
            if tup is None:
                continue
            try:
                _, _, checksum = parse_component_root(tup.root)
            except ValueError:
                continue
            pinned.add(checksum)
    return frozenset(pinned)


def persona_pin_roots(
    *, bindings_dir: Path = Path("/config/bindings"),
    specialists_dir: Path = Path("/config/specialists"),
) -> frozenset[str]:
    """Round-2 addition (finding #8): `cas_pin_roots` only ever pins
    SPECIALIST COMPONENT blobs under `specialists_dir/store/`. Installed
    persona overrides live in a COMPLETELY SEPARATE tree
    (`/config/personas/<persona_id>/<persona_version>/`,
    `persona_install.commit_persona_install`'s write target) with no
    reference-root function of its own — a resident OR specialist actively
    bound to an installed persona via `mode="override"` had no recorded pin
    at all. Scans EVERY InstanceDir under both the resident bindings root
    (`bindings_dir/resident-*`) and the specialist root
    (`specialists_dir/<slug>`), for `active.yaml`/`desired.yaml`/
    `active.prior.yaml`, and records `f"{persona_id}@{persona_version}"` for
    every tuple whose `binding.mode == "override"` — the exact
    `/config/personas/<persona_id>/<persona_version>/` directory that must
    stay referenced.

    Round-3 addition (finding #5): spec §4.4 also pins "the current image
    defaults" — this is NOT limited to whatever a resident's tuple happens
    to reference right now (an override-bound resident's tuple never has
    `mode == "image-default"`, so the scan above alone would silently drop
    the image default the moment EVERY resident is override-bound), and the
    spec's own "offer to reset to the current default" language (§4.4's
    "pinned digest unavailable" clause) requires that reset target to always
    resolve. So every value of `personality_binding.
    IMAGE_DEFAULT_PERSONA_BY_SLOT` (today: `"casa/ellen@0.1.0"`,
    `"casa/tina@0.1.0"`, `"casa/gary@0.1.0"`) is added UNCONDITIONALLY,
    independent of the tuple scan."""
    from personality_binding import IMAGE_DEFAULT_PERSONA_BY_SLOT, load_instance_tuple

    pinned: set[str] = set(IMAGE_DEFAULT_PERSONA_BY_SLOT.values())

    def _scan(root: Path, *, skip_names: frozenset[str] = frozenset()) -> None:
        if not root.is_dir():
            return
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name in skip_names:
                continue
            for filename in ("active.yaml", "desired.yaml", "active.prior.yaml"):
                path = entry / filename
                if not path.is_file():
                    continue
                tup = load_instance_tuple(path)
                if tup is None or tup.binding.mode != "override":
                    continue
                pinned.add(f"{tup.binding.persona_id}@{tup.binding.persona_version}")

    _scan(bindings_dir)
    _scan(specialists_dir, skip_names=frozenset({"store", ".staging", ".roles-overlay"}))
    return frozenset(pinned)
