"""Task N1b, Step 17: InstalledSpecialistIndex.installed_component_role_dirs()."""
from pathlib import Path

import yaml

from specialist_registry import InstalledSpecialistIndex


def _write_cas_role(store_root: Path, checksum_hex: str, slug: str) -> None:
    role_dir = store_root / checksum_hex / "role"
    role_dir.mkdir(parents=True)
    (role_dir / "role.yaml").write_text(
        yaml.safe_dump({"id": f"specialist:{slug}", "kind": "specialist", "slot": slug}),
        encoding="utf-8")
    (role_dir / "doctrine.md").write_text("# Core doctrine\n\nTest.\n", encoding="utf-8")


def test_installed_component_role_dirs_resolves_cas_paths(tmp_path: Path) -> None:
    from specialist_install import commit_specialist_install  # noqa: F401 — import-order sanity
    from specialist_install import component_root_string
    from personality_binding import (
        InstanceDir, InstanceTuple, materialize_component_default_binding,
    )
    from role_slot import RoleSlot, ResolvedModel
    from persona_pack import PersonaPack, PersonaManifest

    specialists_dir = tmp_path / "specialists"
    checksum = "sha256:" + "4" * 64
    _write_cas_role(specialists_dir / "store", "4" * 64, "mtg")

    role = RoleSlot(
        role_id="specialist:mtg", kind="specialist", slot="mtg", mission="x",
        resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                      sdk_model="claude-sonnet-4-6", option=None),
        normalized={}, doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
    )
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
    root = component_root_string(component_id="casa-test/mtg", version="0.1.0",
                                  component_checksum=checksum)
    binding = materialize_component_default_binding(role=role, persona=persona, component_root=root)
    instance_dir = InstanceDir(specialists_dir / "mtg")
    instance_dir.stage_desired(InstanceTuple(
        root=root, binding=binding, config_snapshot={}, config_digest=binding.effective_config_digest))
    instance_dir.commit_desired_to_active()

    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()
    role_dirs = index.installed_component_role_dirs()
    assert role_dirs["mtg"] == specialists_dir / "store" / ("4" * 64)
    assert (role_dirs["mtg"] / "role" / "role.yaml").is_file()


def test_installed_component_role_dirs_includes_pending_configuration_slugs(tmp_path: Path) -> None:
    """A slug with only a `desired` tuple (no `active`) still resolves — the
    accessor itself is not the gate that keeps a pending-configuration
    specialist non-loadable; specialist_materialize's SEPARATE operational-
    file reconcile is (it only acts on `instance.active`)."""
    from specialist_install import component_root_string
    from personality_binding import (
        InstanceDir, InstanceTuple, materialize_component_default_binding,
    )
    from role_slot import RoleSlot, ResolvedModel
    from persona_pack import PersonaPack, PersonaManifest

    specialists_dir = tmp_path / "specialists"
    checksum = "sha256:" + "5" * 64
    _write_cas_role(specialists_dir / "store", "5" * 64, "pending")

    role = RoleSlot(
        role_id="specialist:pending", kind="specialist", slot="pending", mission="x",
        resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                      sdk_model="claude-sonnet-4-6", option=None),
        normalized={}, doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
    )
    persona = PersonaPack(
        persona_id="casa/judge", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Judge", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="adjudicator",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                 "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(), markdown="# Core\n\nJudges rules.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )
    root = component_root_string(component_id="casa-test/pending", version="0.1.0",
                                  component_checksum=checksum)
    binding = materialize_component_default_binding(role=role, persona=persona, component_root=root)
    instance_dir = InstanceDir(specialists_dir / "pending")
    instance_dir.stage_desired(InstanceTuple(
        root=root, binding=binding, config_snapshot={}, config_digest=binding.effective_config_digest))
    # Deliberately never committed — pending-configuration state.

    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()
    role_dirs = index.installed_component_role_dirs()
    assert role_dirs["pending"] == specialists_dir / "store" / ("5" * 64)


def test_installed_component_role_dirs_skips_a_malformed_root(tmp_path: Path) -> None:
    """A corrupt/legacy InstanceTuple whose `root` doesn't parse must be
    skipped, never raise and never resolve to a guessed CAS path."""
    from personality_binding import InstanceDir, InstanceTuple, materialize_component_default_binding
    from role_slot import RoleSlot, ResolvedModel
    from persona_pack import PersonaPack, PersonaManifest

    specialists_dir = tmp_path / "specialists"
    role = RoleSlot(
        role_id="specialist:bad", kind="specialist", slot="bad", mission="x",
        resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                      sdk_model="claude-sonnet-4-6", option=None),
        normalized={}, doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
    )
    persona = PersonaPack(
        persona_id="casa/judge", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Judge", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="adjudicator",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                 "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(), markdown="# Core\n\nJudges rules.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )
    binding = materialize_component_default_binding(
        role=role, persona=persona, component_root="not-a-parseable-root")
    instance_dir = InstanceDir(specialists_dir / "bad")
    instance_dir.stage_desired(InstanceTuple(
        root="not-a-parseable-root", binding=binding, config_snapshot={},
        config_digest=binding.effective_config_digest))
    instance_dir.commit_desired_to_active()

    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()
    assert index.installed_component_role_dirs() == {}
