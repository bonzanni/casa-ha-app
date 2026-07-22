# casa-agent/rootfs/opt/casa/specialist_materialize.py
"""Closes Correction #1: turns an installed component (Task N1a/N1b's CAS
tree under /config/specialists/<slug>/) into the EXACT on-disk shape
agent_loader.load_all_specialists / SpecialistRegistry.load() already read —
the legacy operational file set under /config/agents/specialists/<slug>/,
PLUS a roles overlay directory agent_loader's existing (but never
production-threaded) `roles_dir` parameter can point at."""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from persona_pack import PersonaPack
    from role_slot import RoleSlot

_DEFAULT_OVERLAY_ROOT = Path("/config/specialists/.roles-overlay")


def specialist_roles_overlay_root() -> Path:
    return _DEFAULT_OVERLAY_ROOT


def reconcile_specialist_roles_overlay(
    *, installed_index, overlay_root: "Path | None" = None, image_roles_dir: "str | None" = None,
) -> Path:
    """Rebuild <overlay>/specialist/<slug>/{role.yaml,doctrine.md} for EVERY
    image-bundled specialist role (today: finance, until Task N2's no-gap
    cutover removes it) PLUS every installed specialist's role artifact, so
    ONE roles_dir root serves agent_loader.load_all_specialists for every
    specialist uniformly — residents/executors are unaffected and keep using
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
    pre-this-fix layout (verified: the image ships
    `defaults/agents/specialists/finance/`, which `config_sync` places at
    `/config/agents/specialists/finance/` as an ordinary directory — Task
    N2's no-gap cutover installs `finance` through this exact pipeline,
    so this branch is not hypothetical). Converting a real, non-empty
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
    except Exception:
        shutil.rmtree(content_dir, ignore_errors=True)
        raise

    if slug_dir.is_symlink():
        prior_content_dir = agents_specialists_dir / os.readlink(slug_dir)
        new_link = agents_specialists_dir / f".{slug}.link-{uuid.uuid4().hex}"
        os.symlink(content_dir_name, new_link)
        os.replace(new_link, slug_dir)  # the ONE atomic syscall — slug_dir never absent
        shutil.rmtree(prior_content_dir, ignore_errors=True)  # best-effort GC of the old version
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
