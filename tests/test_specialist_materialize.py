# tests/test_specialist_materialize.py
import os
from pathlib import Path

import yaml

from specialist_materialize import (
    materialize_specialist_operational_files,
    reconcile_specialist_roles_overlay,
)


class _FakeInstalledIndex:
    """Test double — real InstalledSpecialistIndex is exercised in
    tests/test_specialist_registry_roles_overlay.py; this file isolates the
    overlay-building logic from InstanceDir/CAS file I/O."""

    def __init__(self, slugs: dict[str, Path]) -> None:
        self._slugs = slugs  # slug -> component_dir holding role/{role.yaml,doctrine.md}

    def installed_component_role_dirs(self) -> dict[str, Path]:
        return dict(self._slugs)


def _write_role_dir(root: Path, slug: str, kind: str = "specialist") -> Path:
    role_dir = root / "role"
    role_dir.mkdir(parents=True)
    (role_dir / "role.yaml").write_text(
        yaml.safe_dump({"id": f"{kind}:{slug}", "kind": kind, "slot": slug}), encoding="utf-8")
    (role_dir / "doctrine.md").write_text("# Core doctrine\n\nTest.\n", encoding="utf-8")
    return root


def test_overlay_includes_the_bundled_image_specialist(tmp_path: Path) -> None:
    # NOTE: unlike an installed component (whose role artifact lives one
    # level down, under <component_dir>/role/), the image's bundled roles
    # tree is FLAT: defaults/roles/specialist/<slug>/{role.yaml,doctrine.md}
    # directly (verified on disk: defaults/roles/specialist/finance/role.yaml
    # has no intervening "role/" directory) — so this fixture writes flat,
    # not via the nested-`role/` `_write_role_dir` helper the installed-
    # component tests below use.
    image_roles = tmp_path / "image-roles"
    finance_dir = image_roles / "specialist" / "finance"
    finance_dir.mkdir(parents=True)
    (finance_dir / "role.yaml").write_text(
        yaml.safe_dump({"id": "specialist:finance", "kind": "specialist", "slot": "finance"}),
        encoding="utf-8")
    (finance_dir / "doctrine.md").write_text("# Core doctrine\n\nTest.\n", encoding="utf-8")
    overlay_root = tmp_path / "overlay"
    result = reconcile_specialist_roles_overlay(
        installed_index=_FakeInstalledIndex({}), overlay_root=overlay_root,
        image_roles_dir=str(image_roles),
    )
    assert (result / "specialist" / "finance" / "role.yaml").is_file()
    assert (result / "specialist" / "finance" / "doctrine.md").is_file()


def test_overlay_includes_an_installed_specialist(tmp_path: Path) -> None:
    image_roles = tmp_path / "image-roles"
    (image_roles / "specialist").mkdir(parents=True)
    component_dir = tmp_path / "component"
    _write_role_dir(component_dir, "mtg")
    overlay_root = tmp_path / "overlay"
    result = reconcile_specialist_roles_overlay(
        installed_index=_FakeInstalledIndex({"mtg": component_dir}), overlay_root=overlay_root,
        image_roles_dir=str(image_roles),
    )
    assert (result / "specialist" / "mtg" / "role.yaml").is_file()
    installed_role = yaml.safe_load((result / "specialist" / "mtg" / "role.yaml").read_text())
    assert installed_role["slot"] == "mtg"


def test_overlay_is_fully_rebuilt_each_call_never_accretes_stale_entries(tmp_path: Path) -> None:
    image_roles = tmp_path / "image-roles"
    (image_roles / "specialist").mkdir(parents=True)
    overlay_root = tmp_path / "overlay"
    component_a = tmp_path / "a"
    _write_role_dir(component_a, "old-slug")
    reconcile_specialist_roles_overlay(
        installed_index=_FakeInstalledIndex({"old-slug": component_a}), overlay_root=overlay_root,
        image_roles_dir=str(image_roles),
    )
    assert (overlay_root / "specialist" / "old-slug").exists()
    # A reconcile with a DIFFERENT installed set (e.g. after an uninstall)
    # must not leave the old slug behind.
    reconcile_specialist_roles_overlay(
        installed_index=_FakeInstalledIndex({}), overlay_root=overlay_root,
        image_roles_dir=str(image_roles),
    )
    assert not (overlay_root / "specialist" / "old-slug").exists()


def test_materialize_operational_files_writes_the_required_tier_file_set(tmp_path: Path) -> None:
    from role_slot import RoleSlot, ResolvedModel

    role = RoleSlot(
        role_id="specialist:mtg", kind="specialist", slot="mtg",
        mission="Answer MTG rules questions.",
        resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                      sdk_model="claude-sonnet-4-6", option=None),
        normalized={
            "model": {"source": "fixed", "value": "sonnet"},
            "tools": {"allowed": [], "disallowed": ["Bash", "Write", "Edit"],
                       "permission_mode": "dontAsk", "max_turns": 8, "skills": "none",
                       "voice_guard": "none"},
            "mcp_servers": [], "memory": {"token_budget": 0, "read_strategy": "per_turn"},
            "session": {"strategy": "ephemeral", "idle_timeout_seconds": 0},
            "tts": {"tag_dialect": "none", "error_phrases": {}},
            "response": {"text": {"register": "precise"}, "voice": {"register": "spoken"},
                          "restricted_webhook": {"register": "plain"}},
            "requires": {"plugins": ["mtg"], "tools": ["mcp__plugin_mtg_mtg__lookup_rule"]},
        },
        doctrine="# Core doctrine\n\nAnswer questions.\n", checksum="sha256:" + "1" * 64,
    )
    from persona_pack import PersonaPack, PersonaManifest
    persona = PersonaPack(
        persona_id="casa/judge", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Judge", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="adjudicator",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                 "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(), markdown="# Core\n\nJudges rules.\n\n## Negative space\n\nNever guesses.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )
    materialize_specialist_operational_files(
        agents_specialists_dir=tmp_path / "agents-specialists", slug="mtg", role=role, persona=persona,
    )
    slug_dir = tmp_path / "agents-specialists" / "mtg"
    # Round-4 fix (finding #1): slug_dir is a symlink to a versioned content
    # directory, never a real directory the swap has to rmtree/rename.
    assert slug_dir.is_symlink()
    assert slug_dir.is_dir()  # pathlib follows the symlink for is_dir/is_file/open transparently
    for name in ("character.yaml", "voice.yaml", "response_shape.yaml", "runtime.yaml"):
        assert (slug_dir / name).is_file(), name
    runtime = yaml.safe_load((slug_dir / "runtime.yaml").read_text())
    assert runtime["kind"] == "specialist"
    assert runtime["model"] == {"source": "fixed", "value": "sonnet"}
    assert runtime["channels"] == []
    assert runtime["session"]["strategy"] == "ephemeral"
    assert runtime["requires"]["plugins"] == ["mtg"]
    character = yaml.safe_load((slug_dir / "character.yaml").read_text())
    assert character["name"] == "Judge"
    assert character["role"] == "mtg"


def test_materialize_operational_files_repeat_call_swaps_atomically_and_gcs_old_version(
    tmp_path: Path,
) -> None:
    """Round-4 fix (finding #1): a second materialize call for the SAME slug
    retargets the slug_dir symlink in one os.replace — never leaves slug_dir
    absent, never leaves the old content directory behind, and the new
    content fully replaces (not merges with) the old."""
    from role_slot import RoleSlot, ResolvedModel
    from persona_pack import PersonaPack, PersonaManifest

    def _role(archetype_marker: str) -> "RoleSlot":
        return RoleSlot(
            role_id="specialist:mtg", kind="specialist", slot="mtg", mission="x",
            resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                          sdk_model="claude-sonnet-4-6", option=None),
            normalized={}, doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
        )

    def _persona(archetype: str) -> "PersonaPack":
        return PersonaPack(
            persona_id="casa/judge", version="0.1.0", trait_schema_version=1,
            identity={"display_name": "Judge", "pronouns": {
                "subject": "they", "object": "them", "possessive_adjective": "their",
                "possessive_pronoun": "theirs", "reflexive": "themself"}},
            relationship_posture="established", archetype=archetype,
            traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                     "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
            quirks=(), markdown="# Core\n\nJudges rules.\n", examples=(),
            manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
            checksum="sha256:" + "2" * 64,
        )

    agents_specialists_dir = tmp_path / "agents-specialists"
    materialize_specialist_operational_files(
        agents_specialists_dir=agents_specialists_dir, slug="mtg",
        role=_role("adjudicator"), persona=_persona("adjudicator"))
    slug_dir = agents_specialists_dir / "mtg"
    first_target = os.readlink(slug_dir)
    assert (agents_specialists_dir / first_target).is_dir()

    materialize_specialist_operational_files(
        agents_specialists_dir=agents_specialists_dir, slug="mtg",
        role=_role("mentor"), persona=_persona("mentor"))
    second_target = os.readlink(slug_dir)
    assert second_target != first_target  # retargeted to a fresh content dir
    assert not (agents_specialists_dir / first_target).exists()  # old version GC'd
    voice = yaml.safe_load((slug_dir / "voice.yaml").read_text())
    assert voice["tone"] == ["mentor"]  # content fully replaced, not merged


def test_materialize_operational_files_migrates_a_legacy_real_directory_slug_dir(
    tmp_path: Path,
) -> None:
    """Round-4 fix (finding #1)'s documented one-time exception: an
    image-provided REAL (non-symlink) slug_dir — e.g. the bundled `finance`
    specialist's pre-cutover layout — is migrated into the symlink scheme on
    its first materialize call, and every call after that goes through the
    ordinary single-os.replace path."""
    from role_slot import RoleSlot, ResolvedModel
    from persona_pack import PersonaPack, PersonaManifest

    role = RoleSlot(
        role_id="specialist:finance", kind="specialist", slot="finance", mission="x",
        resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                      sdk_model="claude-sonnet-4-6", option=None),
        normalized={}, doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
    )
    persona = PersonaPack(
        persona_id="casa/finance", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Finance", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="advisor",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                 "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(), markdown="# Core\n", examples=(),
        manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )
    agents_specialists_dir = tmp_path / "agents-specialists"
    legacy_dir = agents_specialists_dir / "finance"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "character.yaml").write_text("legacy: true\n", encoding="utf-8")

    materialize_specialist_operational_files(
        agents_specialists_dir=agents_specialists_dir, slug="finance", role=role, persona=persona)

    assert legacy_dir.is_symlink()  # migrated, not left as a real dir
    character = yaml.safe_load((legacy_dir / "character.yaml").read_text())
    assert character["name"] == "Finance"  # legacy content replaced, not merged
    # No stray `.finance.prior-*` backup left behind on success.
    assert not any(p.name.startswith(".finance.prior-") for p in agents_specialists_dir.iterdir())


def test_materialized_finance_specialist_round_trips_through_the_real_loader(
    tmp_path: Path,
) -> None:
    """N1b slice A regression (task-review Critical): the operational files
    ``_write_specialist_operational_files`` writes must be schema-valid for
    the REAL ``agent_loader.load_agent_from_dir`` — the module's entire
    purpose — not merely internally consistent with the writer's own
    assumptions. Exercises the full pipeline against the REAL bundled
    finance role artifact (``defaults/roles/specialist/finance``) plus a
    real-shaped persona, materializes operational files, builds a real
    roles-overlay via ``reconcile_specialist_roles_overlay``, and then
    calls the REAL loader. Must FAIL on the pre-fix writer: role.yaml's
    ``session.idle_timeout_seconds`` / ``tts.error_phrases`` violate
    ``runtime.v1.json``'s ``additionalProperties: false`` sub-schemas, and
    the hardcoded ``card: ""`` / ``prompt: ""`` violate
    ``character.v1.json``'s ``minLength: 1``."""
    import agent_loader
    import role_artifact
    import role_slot
    from persona_pack import PersonaManifest, PersonaPack

    code_root = Path(agent_loader.__file__).resolve().parent
    finance_role_dir = code_root / "defaults" / "roles" / "specialist" / "finance"
    source = role_artifact.load_role_artifact(finance_role_dir)
    role = role_slot.materialize_role(source=source, options={})

    persona = PersonaPack(
        persona_id="casa/alex", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Alex", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="advisor",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                 "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(),
        markdown="# Core\n\nHandles household finances precisely.\n\n"
                 "## Negative space\n\nNever guesses at numbers.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )

    agents_specialists_dir = tmp_path / "agents-specialists"
    materialize_specialist_operational_files(
        agents_specialists_dir=agents_specialists_dir, slug="finance",
        role=role, persona=persona,
    )
    slug_dir = agents_specialists_dir / "finance"

    overlay_root = tmp_path / "overlay"
    reconcile_specialist_roles_overlay(
        installed_index=_FakeInstalledIndex({}), overlay_root=overlay_root,
        image_roles_dir=str(code_root / "defaults" / "roles"),
    )

    cfg = agent_loader.load_agent_from_dir(
        str(slug_dir), policies=None, roles_dir=str(overlay_root),
    )

    assert cfg.role == "finance"
    assert cfg.kind == "specialist"
    assert cfg.role_artifact.role["id"] == "specialist:finance"
