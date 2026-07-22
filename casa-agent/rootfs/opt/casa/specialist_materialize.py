# casa-agent/rootfs/opt/casa/specialist_materialize.py
"""Closes Correction #1: turns an installed component (Task N1a/N1b's CAS
tree under /config/specialists/<slug>/) into the EXACT on-disk shape
agent_loader.load_all_specialists / SpecialistRegistry.load() already read —
the legacy operational file set under /config/agents/specialists/<slug>/,
PLUS a roles overlay directory agent_loader's existing (but never
production-threaded) `roles_dir` parameter can point at."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from persona_pack import PersonaPack
    from role_slot import RoleSlot
    from specialist_registry import InstalledSpecialistIndex

_DEFAULT_OVERLAY_ROOT = Path("/config/specialists/.roles-overlay")

# Whole-branch review F3 (round 2): serializes the tuple-commit+materialize
# sequence (commit_specialist_install/upgrade_specialist/rollback_specialist)
# against the self-heal reconcile loop's per-slug re-read+materialize. A single
# process-wide lock — contention is negligible (installs are rare operator
# actions; the self-heal loop does only bounded local file IO) — so a per-slug
# lock table would add machinery for no measurable benefit. It is held ONLY
# across (a) a commit function's commit_desired_to_active()+materialize and
# (b) one self-heal slug's in-lock re-read+materialize; NEVER across any
# network fetch, git resolve, or prompt compile (those all complete before the
# lock is taken). It is never acquired recursively and no second lock is ever
# taken while holding it, so it introduces no deadlock or lock-ordering
# surface. See `_reconcile_specialist_operational_files` for the race it
# closes.
MATERIALIZE_LOCK = threading.Lock()

# F5 (fail-closed self-heal): a small marker dotfile written INTO each content
# directory recording the (binding_digest, component_root) the op-files were
# materialized from. The self-heal loop reads it to decide whether an
# already-on-disk op-dir is current-by-marker (origin-provenance, not content
# integrity — see _check_file_set backstop) for the active tuple; a dotfile is
# skipped by agent_loader._check_file_set, so it never trips the loader's
# unknown-file gate.
#
# F6 (round 2, ADJUDICATED ACCEPT — scope of the guarantee): the marker proves
# WHICH tuple these op-files were materialized from (origin provenance), NOT
# that the bytes on disk still match what materialize wrote. A file that is
# corrupted-but-marker-intact (in-place tampering) is deliberately out of this
# marker's scope — that threat is covered elsewhere: MISSING files trip
# agent_loader's _check_file_set (a loud, per-slug LoadError backstop at load
# time), and aggressive removal of a marker-current op-dir on a merely
# transient re-materialize failure is exactly the over-removal the F5 design
# avoids (a benign retry, not a capability-drift, when the marker still matches).
_BINDING_MARKER_NAME = ".binding-digest"


def resolve_material_content_dir(link_path: Path, agents_specialists_dir: Path) -> "Path | None":
    """Return the content directory a materialize symlink at *link_path*
    points at — ONLY if its target is a SAFE relative single-segment
    `.{slug}.material-<32-hex>` name that resolves strictly inside
    *agents_specialists_dir*. Returns None on any containment violation
    (absolute target, separators, `..`, wrong shape, or an escaping resolved
    path); the caller must then unlink ONLY the symlink and NEVER rmtree its
    target (F2).

    Whole-branch review F4 (round 2, cross-slug GC containment): the op-dir
    symlink lives AT ``agents_specialists_dir / <slug>``, so ``link_path.name``
    IS the slug this link may legitimately materialize. The target must be
    THIS slug's own ``.{slug}.material-<32-hex>`` — NOT any other slug's
    content directory. Without this per-slug binding, an uninstall or
    re-materialize of slug A whose symlink was cross-pointed at slug B's
    ``.{B}.material-...`` dir would GC B's LIVE content. Parameterizing the
    shape gate by the expected slug fails such a cross-pointed link closed
    (returns None -> caller unlinks the symlink only, never rmtree's B's dir)."""
    try:
        target = os.readlink(link_path)
    except OSError:
        return None
    if (os.path.isabs(target) or os.sep in target
            or (os.altsep and os.altsep in target)
            or target in ("", ".", "..")):
        return None
    # F4: require the target to be THIS slug's content dir, never a foreign one.
    expected_slug = link_path.name
    if re.fullmatch(rf"\.{re.escape(expected_slug)}\.material-[0-9a-f]{{32}}", target) is None:
        return None
    content_dir = agents_specialists_dir / target
    try:
        resolved = content_dir.resolve()
        root = agents_specialists_dir.resolve()
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    return content_dir


def _write_binding_marker(
    content_dir: Path, *, binding_digest: "str | None", component_root: "str | None",
) -> None:
    payload = {"binding_digest": binding_digest or "", "root": component_root or ""}
    (content_dir / _BINDING_MARKER_NAME).write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _read_binding_marker(slug_dir: Path) -> "dict | None":
    try:
        raw = json.loads((slug_dir / _BINDING_MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def specialist_roles_overlay_root() -> Path:
    return _DEFAULT_OVERLAY_ROOT


def reconcile_specialist_roles_overlay(
    *, installed_index, overlay_root: "Path | None" = None, image_roles_dir: "str | None" = None,
) -> Path:
    """Rebuild <overlay>/specialist/<slug>/{role.yaml,doctrine.md} for EVERY
    image-bundled specialist role (Task N2's no-gap cutover means this is
    normally empty — specialists install from a repository, never authored
    in-image — but the scan stays kind-general, not hard-coded to zero)
    PLUS every installed specialist's role artifact, so ONE roles_dir root
    serves agent_loader.load_all_specialists for every specialist
    uniformly — residents/executors are unaffected and keep using
    agent_loader.DEFAULT_ROLES_DIR untouched. Deterministic + idempotent:
    fully rebuilt from source of truth on every call (image tree +
    `installed_index.installed_component_role_dirs()`), never hand-edited —
    a slug present in a STALE overlay but no longer installed (an uninstall)
    is removed, never left dangling (verified by
    test_overlay_is_fully_rebuilt_each_call_never_accretes_stale_entries)."""
    from agent_loader import DEFAULT_ROLES_DIR

    root = Path(overlay_root) if overlay_root is not None else specialist_roles_overlay_root()
    specialist_overlay = root / "specialist"
    if specialist_overlay.exists():
        shutil.rmtree(specialist_overlay)
    specialist_overlay.mkdir(parents=True, mode=0o700)

    image_base = Path(image_roles_dir or DEFAULT_ROLES_DIR) / "specialist"
    if image_base.is_dir():
        for role_dir in sorted(p for p in image_base.iterdir() if p.is_dir()):
            _copy_role_dir(role_dir, specialist_overlay / role_dir.name)

    for slug, component_role_dir in sorted(
        installed_index.installed_component_role_dirs().items()
    ):
        src = Path(component_role_dir) / "role"
        _copy_role_dir(src, specialist_overlay / slug)

    return root


def _copy_role_dir(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("role.yaml", "doctrine.md"):
        source_file = src / name
        if not source_file.is_file():
            raise ValueError(f"{src}: missing required role-artifact file {name!r}")
        (dest / name).write_bytes(source_file.read_bytes())


def _voice_from_persona(persona: "PersonaPack") -> dict:
    # A small, honest voice.yaml projection — the ACTUAL system prompt
    # served to the SDK comes from cfg.compiled_prompt_bundle (Task N1b's
    # agent_loader wiring below), so this legacy field exists to satisfy
    # TIER_FILES["specialist"]["required"] and to keep any future reader of
    # cfg.voice consistent with the persona actually bound, never a stub.
    return {
        "schema_version": 1,
        "tone": [persona.archetype] if persona.archetype else [],
        "cadence": "natural",
        "forbidden_patterns": [],
        "signature_phrases": {},
    }


def materialize_specialist_operational_files(
    *, agents_specialists_dir: Path, slug: str, role: "RoleSlot", persona: "PersonaPack",
    binding_digest: "str | None" = None, component_root: "str | None" = None,
) -> None:
    """Write the four TIER_FILES['specialist']['required'] legacy files,
    derived from the compiled role.normalized + persona — never
    hand-authored, never containing the actual served prompt (that is
    cfg.compiled_prompt_bundle, wired in agent_loader.py below).

    Round-2 fix (finding #7): writes the complete 4-file set into a
    slug-scoped TEMP directory first, before touching the live `slug_dir` at
    all, so a write failure never touches what's already on disk.

    Round-4 fix (this review pass, finding #1 — supersedes round 3's
    two-step rename below). Verified empirically: `os.replace(new_dir,
    existing_nonempty_dir)` raises `OSError: [Errno 39] Directory not
    empty` — POSIX `rename(2)` only replaces a directory target if it is
    EMPTY. That means NO scheme that swaps the two REAL, populated content
    directories under the one stable name `slug_dir` can ever be a single
    atomic syscall; round 3's "rename old aside, then rename new in" is the
    closest that gets, and it still has a real window — between those two
    renames the PATH `slug_dir` resolves to nothing, so a concurrent
    `agent_loader.load_all_specialists` scan (or a crash exactly there) sees
    ENOENT, not "old" or "new".

    Fixed by adding one level of indirection: `slug_dir` is no longer a real
    directory that gets swapped — it is a **symlink** that gets
    *retargeted*. The actual files live in a uniquely-named content
    directory, `agents_specialists_dir / f".{slug}.material-<uuid4hex>"`,
    that this call NEVER reuses (a fresh one every call, exactly like the
    round-2 staging dir). Retargeting a symlink is swapping ONE directory
    ENTRY — not a directory's contents — so `os.replace(new_symlink,
    slug_dir)` is unconditionally a single atomic syscall regardless of
    whether `slug_dir` already exists or what it currently points at (a
    symlink is never "a non-empty directory" to `rename(2)`). The live swap
    is therefore exactly one `os.replace` call: `slug_dir`, as a path,
    resolves to the fully-populated OLD content directory right up until the
    instant it resolves to the fully-populated NEW one — never absent, never
    partially written, and no rollback COPY is needed, because the OLD
    content directory is never touched by the swap itself: it simply keeps
    existing under its own versioned name (a real, cheap "backup" for free)
    until this function garbage-collects it AFTER the swap succeeds. If the
    single `os.replace` itself fails, the old symlink target is completely
    unchanged — there is nothing to restore.

    One documented, unavoidable exception: the very FIRST call for a slug
    whose `slug_dir` is still a REAL, non-symlink directory from the
    pre-this-fix layout (prior image versions shipped
    `defaults/agents/specialists/finance/`, which `config_sync` placed at
    `/config/agents/specialists/finance/` as an ordinary directory — a
    deployment upgrading across Task N2's no-gap cutover re-installs
    `finance` through this exact pipeline, so this branch is not
    hypothetical). Converting a real, non-empty
    directory's NAME into a symlink cannot be done as a single syscall
    either (verified: replacing a real non-empty directory with a symlink
    also raises `OSError`, `EISDIR`/`ENOTEMPTY`-class — a directory-to-
    non-directory replace is exactly as restricted as the directory-to-
    directory case above). This ONE-TIME migration therefore still uses a
    round-3-style two-step rename (real dir aside, symlink in, matching
    fallback-to-restore on failure) and — like round 3's original fix —
    has a brief window where `slug_dir` does not exist. This is accepted as
    a bounded, one-time-per-slug, boot-time transition (finance's cutover
    runs before the channel/bus loop starts, per Correction #1's boot-
    ordering citation — no concurrent reader exists at that moment in
    practice), never repeated: every subsequent materialize call for that
    slug finds `slug_dir` already a symlink and takes the pure
    single-`os.replace` path above."""
    import uuid

    agents_specialists_dir = Path(agents_specialists_dir)
    agents_specialists_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    slug_dir = agents_specialists_dir / slug
    content_dir_name = f".{slug}.material-{uuid.uuid4().hex}"
    content_dir = agents_specialists_dir / content_dir_name
    content_dir.mkdir(parents=True, mode=0o700)
    try:
        _write_specialist_operational_files(content_dir, slug=slug, role=role, persona=persona)
        # F5: record which (binding_digest, root) these op-files were built
        # from, so the self-heal loop can tell a CURRENT op-dir from a stale
        # one when a later re-materialize fails.
        _write_binding_marker(
            content_dir, binding_digest=binding_digest, component_root=component_root)
    except Exception:
        shutil.rmtree(content_dir, ignore_errors=True)
        raise

    if slug_dir.is_symlink():
        # F2: the prior symlink target becomes a `shutil.rmtree` target below.
        # Resolve it through the containment gate — a non-contained target
        # (absolute/escaping) is NOT rmtree'd; we still retarget the symlink.
        prior_content_dir = resolve_material_content_dir(slug_dir, agents_specialists_dir)
        new_link = agents_specialists_dir / f".{slug}.link-{uuid.uuid4().hex}"
        os.symlink(content_dir_name, new_link)
        os.replace(new_link, slug_dir)  # the ONE atomic syscall — slug_dir never absent
        if prior_content_dir is not None:
            shutil.rmtree(prior_content_dir, ignore_errors=True)  # best-effort GC of the old version
        else:
            logger.warning(
                "materialize %r: prior operational symlink target failed containment; retargeted "
                "the symlink but left its (out-of-tree) target untouched", slug)
        return

    if not slug_dir.exists():
        new_link = agents_specialists_dir / f".{slug}.link-{uuid.uuid4().hex}"
        os.symlink(content_dir_name, new_link)
        os.replace(new_link, slug_dir)
        return

    # One-time legacy migration (see docstring): slug_dir exists as a REAL
    # directory, not yet a symlink. Bounded, documented exception to the
    # "never absent" guarantee above.
    backup_dir = agents_specialists_dir / f".{slug}.prior-{uuid.uuid4().hex}"
    os.replace(slug_dir, backup_dir)  # atomic: old real dir out of the way, slug_dir now absent
    try:
        new_link = agents_specialists_dir / f".{slug}.link-{uuid.uuid4().hex}"
        os.symlink(content_dir_name, new_link)
        os.replace(new_link, slug_dir)  # atomic: slug_dir now a symlink to the new content
    except Exception:
        os.replace(backup_dir, slug_dir)  # restore — slug_dir must never stay absent
        raise
    shutil.rmtree(backup_dir, ignore_errors=True)


def _map_session(session: dict) -> dict:
    """role.yaml's session sub-schema (``defaults/schema/role.v1.json``
    ``session``) requires ``{strategy, idle_timeout_seconds}``. The
    OPERATIONAL ``runtime.v1.json`` ``session`` sub-schema allows only
    ``{strategy, idle_timeout}`` with ``additionalProperties: false`` — a
    straight pass-through of the role shape is a schema violation (field
    NAME, not just value). Map the field name; never pass the role-shape
    key through verbatim."""
    out: dict = {}
    if "strategy" in session:
        out["strategy"] = session["strategy"]
    if "idle_timeout_seconds" in session:
        out["idle_timeout"] = session["idle_timeout_seconds"]
    elif "idle_timeout" in session:
        out["idle_timeout"] = session["idle_timeout"]
    return out


def _map_tts(tts: dict) -> tuple[dict, dict]:
    """role.yaml's tts sub-schema requires ``{tag_dialect, error_phrases}``.
    The operational ``runtime.v1.json`` ``tts`` sub-schema allows ONLY
    ``tag_dialect`` (``additionalProperties: false``) — there is no home
    for ``error_phrases`` inside ``tts`` at the operational layer. The
    loader's actual consumer of voice-error strings is the SIBLING
    top-level ``runtime.yaml`` key ``voice_errors`` (``runtime.v1.json``
    ``voice_errors``; consumed by ``agent_loader._build_runtime_fields`` ->
    ``cfg.voice_errors``), so ``error_phrases`` moves there instead of
    being dropped. Returns ``(tts_out, voice_errors_out)``."""
    tts_out = {"tag_dialect": tts.get("tag_dialect", "square_brackets")}
    voice_errors_out = dict(tts.get("error_phrases") or {})
    return tts_out, voice_errors_out


def _map_response_register(role_register: str) -> str:
    """role.yaml's ``response.text.register`` (``role.v1.json``
    ``responseProjection.register``) is a free descriptive string
    (``minLength: 1`` only, e.g. ``precise``). The operational
    ``response_shape.v1.json`` ``register`` is a coarse channel-modality
    enum (``["spoken", "written"]``) — role.yaml's ``response.text`` block
    is, by construction, the TEXT/written-channel projection (as opposed
    to its sibling ``response.voice`` block), so any descriptive value
    other than the schema's own ``spoken`` literal maps to ``written``,
    never passed through unmapped."""
    return "spoken" if role_register == "spoken" else "written"


def _character_card(persona: "PersonaPack", role: "RoleSlot", display_name: str) -> str:
    """Honest, non-stub ``character.yaml`` ``card`` (``character.v1.json``
    requires ``minLength: 1`` — never a placeholder). Derived from the
    persona's display name plus the role's own mission statement (
    ``role.v1.json`` ``mission``, always non-empty)."""
    return f"{display_name} — the {role.slot} specialist. {role.mission}".strip()


def _character_prompt(role: "RoleSlot") -> str:
    """Honest, non-stub ``character.yaml`` ``prompt`` (``character.v1.json``
    requires ``minLength: 1``). The role's doctrine (``role.doctrine``,
    guaranteed non-empty by ``role_artifact.load_role_artifact``) is the
    closest legacy-semantic equivalent to a system prompt available at
    materialize time — note the ACTUALLY-served prompt for an active
    specialist is the compiled bundle via the tools.py seam of a later
    slice; this field is only the legacy fallback path and must never be
    placeholder junk."""
    return role.doctrine


def _write_specialist_operational_files(
    slug_dir: Path, *, slug: str, role: "RoleSlot", persona: "PersonaPack",
) -> None:
    normalized = role.normalized
    display_name = persona.identity.get("display_name", slug)

    character = {
        "schema_version": 1, "name": display_name,
        "role": slug, "archetype": "specialist",
        "card": _character_card(persona, role, display_name),
        "prompt": _character_prompt(role),
    }
    (slug_dir / "character.yaml").write_text(
        yaml.safe_dump(character, sort_keys=False), encoding="utf-8")
    (slug_dir / "voice.yaml").write_text(
        yaml.safe_dump(_voice_from_persona(persona), sort_keys=False), encoding="utf-8")

    response = normalized.get("response", {})
    response_shape = {
        "schema_version": 1,
        "max_sentences_confirmation": response.get("text", {}).get(
            "max_confirmation_sentences", 2),
        "max_sentences_status": response.get("text", {}).get("max_status_sentences", 3),
        "register": _map_response_register(response.get("text", {}).get("register", "written")),
        "format": "plain", "rules": [],
    }
    (slug_dir / "response_shape.yaml").write_text(
        yaml.safe_dump(response_shape, sort_keys=False), encoding="utf-8")

    tts, voice_errors = _map_tts(dict(normalized.get("tts", {})))
    runtime = {
        "schema_version": 1, "kind": "specialist", "model": dict(normalized.get("model", {})),
        "enabled": True, "tools": dict(normalized.get("tools", {})),
        "mcp_server_names": list(normalized.get("mcp_servers", [])),
        "memory": dict(normalized.get("memory", {})), "channels": [],
        "session": _map_session(dict(normalized.get("session", {}))), "tts": tts,
        "voice_errors": voice_errors, "cwd": "", "requires": dict(normalized.get("requires", {})),
    }
    (slug_dir / "runtime.yaml").write_text(
        yaml.safe_dump(runtime, sort_keys=False), encoding="utf-8")


def current_specialist_roles_dir(
    installed_index: "InstalledSpecialistIndex | None" = None,
    *,
    specialists_dir: Path = Path("/config/specialists"),
    agents_specialists_dir: Path = Path("/config/agents/specialists"),
) -> str:
    """The ONE function every specialist load/reload call site uses to get
    `roles_dir` (Round-2, finding #1). Freshly loads an `InstalledSpecialistIndex`
    (or reuses the caller's, e.g. the one `set_active_installed_index` already
    tracks) and reconciles the overlay — cheap (a `shutil.rmtree` + copy of a
    handful of small role.yaml/doctrine.md files) and safe to redo on EVERY
    call, exactly matching `reconcile_specialist_roles_overlay`'s own
    'fully rebuilt from source of truth on every call, never accretes stale
    entries' contract. No caller needs to separately track an overlay path —
    it always reflects CURRENTLY installed specialists at call time.

    Round-4 fix (this review pass, finding #2): in addition to the roles
    overlay, this is now ALSO the self-healing seam for the legacy
    TIER_FILES 4-file operational set (character.yaml/voice.yaml/
    response_shape.yaml/runtime.yaml) `materialize_specialist_operational_
    files` writes. `commit_specialist_install`/`upgrade_specialist`/
    `rollback_specialist` (specialist_install.py) commit the InstanceDir
    tuple to `active.yaml` FIRST — the single authoritative, atomically-
    written source of truth — and only THEN materialize the operational
    files as a best-effort side effect (a failure there is caught and
    logged, never rolled back). A crash or write failure between those two
    steps can therefore leave an ACTIVELY-committed slug with stale or
    missing operational files. This function closes that gap
    deterministically and unconditionally, every call: for every slug with
    an ACTIVE tuple (never a slug with only a `desired` tuple —
    pending-configuration specialists must stay non-loadable, matching
    `commit_specialist_install`'s own invariant), it re-derives role+
    persona from the SAME CAS-persisted bytes the active tuple's `root`
    references and calls `materialize_specialist_operational_files` again
    — an idempotent, deterministic rebuild FROM the tuple, never a diff or
    patch. One slug's re-materialize failure is caught and logged
    (mirroring `load_all_specialists`/`load_all_executors`'s per-entry
    isolation) so it can never block reconciling every OTHER installed
    slug, the roles-overlay rebuild, or the caller's reload/boot — that one
    slug simply stays stale until the NEXT reconcile call, which is exactly
    the self-healing property this function exists to provide.

    HERMETICITY FIX (N1b slice-B controller resolution, disclosed in that
    slice's report): passes `overlay_root=specialists_dir / ".roles-overlay"`
    explicitly to `reconcile_specialist_roles_overlay` — identical to the
    DEFAULT overlay root in production (where `specialists_dir` defaults to
    `/config/specialists`, matching `specialist_roles_overlay_root()`'s own
    `/config/specialists/.roles-overlay`), but correct whenever a caller
    (e.g. a test, or a future `specialists_dir` override) passes a different
    `specialists_dir` — the un-parameterized default would otherwise silently
    write to the real `/config/specialists/.roles-overlay` regardless."""
    from specialist_registry import InstalledSpecialistIndex

    index = installed_index
    if index is None:
        index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
        index.load()

    _reconcile_specialist_operational_files(
        installed_index=index, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_specialists_dir,
    )
    return str(reconcile_specialist_roles_overlay(
        installed_index=index, overlay_root=specialists_dir / ".roles-overlay",
    ))


def _reconcile_specialist_operational_files(
    *, installed_index: "InstalledSpecialistIndex", specialists_dir: Path, agents_specialists_dir: Path,
) -> None:
    """Round-4 fix (finding #2)'s per-slug self-heal loop — see
    `current_specialist_roles_dir`'s docstring for the full rationale.
    Deliberately SEPARATE from `reconcile_specialist_roles_overlay`: that
    function serves every installed-OR-pending slug (publishing
    role.yaml/doctrine.md for a not-yet-active candidate is harmless — it
    is never enough by itself to make agent_loader.load_all_specialists
    treat the slug as loadable); this one serves ONLY slugs with a
    committed `active` tuple, matching `commit_specialist_install`'s "a
    pending-configuration candidate must not appear loadable" invariant
    exactly — regenerating legacy op-files for a pending-configuration slug
    would make it loadable and would be a real regression, not a
    self-heal."""
    from role_slot import materialize_role
    from role_artifact import load_role_artifact
    from persona_pack import load_persona_pack
    from personality_binding import InstanceDir
    from specialist_install import parse_component_root, cas_store_dir

    for slug in sorted(installed_index.installed_slugs()):
        instance = installed_index.get_instance(slug)
        if instance is None or instance.active is None:
            continue  # pending-configuration/error: never materialize, never loadable
        # F3 (reconcile-vs-commit race): `installed_index` was snapshotted by
        # the caller; a concurrent upgrade/commit/rollback (also under
        # MATERIALIZE_LOCK) may have committed a NEWER active tuple since. Hold
        # MATERIALIZE_LOCK across this slug's re-read+materialize, and RE-READ
        # the active tuple FROM DISK inside the lock immediately before
        # materializing — so we can never write a stale snapshot's op-files
        # over the freshly-committed binding's (new binding paired with old
        # capabilities until the next reconcile). The lock is released between
        # slugs so a concurrent commit interleaves cleanly; it is never held
        # across the persona/role loads themselves acquiring any other lock.
        with MATERIALIZE_LOCK:
            active = InstanceDir(specialists_dir / slug).active()
            if active is None:
                continue  # uninstalled out from under the snapshot; nothing to heal
            try:
                _, _, checksum = parse_component_root(active.root)
                cas_dir = cas_store_dir(checksum, store_root=specialists_dir / "store")
                if active.binding.mode == "override":
                    # N1d-coupled path (kept verbatim from the brief's draft,
                    # disclosed in the N1b slice-B report): this branch is
                    # unreachable until Plan 2's N1d builds a way to install an
                    # 'override'-mode binding for a specialist — today,
                    # commit_specialist_install only ever produces
                    # 'component-default' bindings. personas_root is fixed at
                    # /config/personas, the persona-install tree N1d will add.
                    personas_root = Path("/config/personas")
                    persona = load_persona_pack(
                        personas_root / active.binding.persona_id
                        / active.binding.persona_version / "pack",
                        personas_root / active.binding.persona_id
                        / active.binding.persona_version / "manifest.json",
                    )
                else:
                    persona = load_persona_pack(
                        cas_dir / "persona" / "pack", cas_dir / "persona" / "manifest.json")
                role = materialize_role(source=load_role_artifact(cas_dir / "role"), options={})
                materialize_specialist_operational_files(
                    agents_specialists_dir=agents_specialists_dir, slug=slug, role=role, persona=persona,
                    binding_digest=active.binding.binding_digest, component_root=active.root)
            except Exception:  # noqa: BLE001 — one slug's failure must never block its siblings/the caller
                # F5 (fail-closed self-heal). A re-materialize failure could
                # leave an ACTIVELY-committed slug paired with STALE
                # operational files (e.g. runtime.yaml's tools allowlist from a
                # superseded tuple) — an approved capability change that
                # silently never took effect. The marker records ORIGIN
                # provenance (which tuple these op-files were built from), NOT
                # content integrity — see the F6 note in `_write_binding_marker`.
                # Fail closed: if the on-disk op-dir is NOT current-by-marker
                # for the active tuple (marker absent or mismatched), drop the
                # slug's op symlink so it becomes loudly UNLOADABLE
                # (agent_loader can no longer find its directory) rather than
                # run with stale capabilities. If the marker MATCHES the active
                # tuple, the files are current-by-origin and a warning
                # suffices; a corrupted-but-marker-intact file (in-place
                # tampering, outside the threat model) is instead caught loudly
                # by agent_loader._check_file_set at load time.
                slug_dir = agents_specialists_dir / slug
                marker = _read_binding_marker(slug_dir) if slug_dir.exists() else None
                is_current = (
                    marker is not None
                    and marker.get("binding_digest") == active.binding.binding_digest
                    and marker.get("root") == active.root
                )
                if is_current:
                    logger.warning(
                        "specialist %r: operational-file re-materialize failed but the on-disk "
                        "marker is current for the active tuple; keeping existing files "
                        "(will retry next call)", slug, exc_info=True)
                else:
                    logger.error(
                        "specialist %r: operational-file re-materialize failed AND on-disk files are "
                        "stale/missing for the active tuple (marker mismatch) — removing the op "
                        "symlink so the slug drops out of the registry rather than run with stale "
                        "capabilities", slug, exc_info=True)
                    try:
                        if slug_dir.is_symlink():
                            slug_dir.unlink()  # F2: unlink the symlink only, never its target
                        elif slug_dir.exists():
                            shutil.rmtree(slug_dir, ignore_errors=True)  # legacy real-dir layout
                    except OSError:
                        logger.error(
                            "specialist %r: failed to remove stale operational directory",
                            slug, exc_info=True)
