"""Personality Phase A, Task 8: boot-time resident binding reconciliation.

reconcile_resident_binding is the SINGLE commit path for a resident's
active binding. The regressions here pin the core fix: a swap/reset staged
into desired.yaml BEFORE a restart is read, validated, and committed on the
next reconcile — not silently discarded — and an invalid staged candidate
leaves the last-known-good active binding running instead of crash-looping.
"""

from __future__ import annotations

from personality_binding import (
    IMAGE_DEFAULT_PERSONA_BY_SLOT,
    InstanceDir,
    InstanceTuple,
    materialize_override_binding,
    reconcile_resident_binding,
)
from persona_pack import PersonaManifest, PersonaPack
from role_slot import ResolvedModel, RoleSlot


def _role() -> RoleSlot:
    resolved = ResolvedModel(
        source="ha_option", effective="haiku", sdk_model="claude-haiku-4-5",
        option="voice_agent_model",
    )
    return RoleSlot(
        role_id="resident:butler", kind="resident", slot="butler",
        mission="Control the household.", resolved_model=resolved,
        normalized={"id": "resident:butler", "persona": {
            "policy": "required",
            "compatibility": ["casa/tina@>=0.1.0 <1.0.0", "casa/gary@>=0.1.0 <1.0.0"]}},
        doctrine="# Core doctrine\n\nControl things.\n", checksum="sha256:" + "1" * 64,
    )


def _persona(persona_id: str, version: str) -> PersonaPack:
    return PersonaPack(
        persona_id=persona_id, version=version, trait_schema_version=1,
        identity={"display_name": persona_id.split("/")[-1].title(), "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="housekeeper",
        traits={"warmth": 3, "formality": 2, "candor": 4, "attunement": 4,
                "curiosity": 3, "levity": 2, "social_energy": 3, "optimism": 3},
        quirks=(), markdown="# Core\n\nKeeps the house running.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )


def _loaders(personas: dict[str, PersonaPack]):
    def load(ref: str) -> PersonaPack:
        if ref not in personas:
            raise ValueError(f"persona {ref!r} unavailable")
        return personas[ref]
    return load


def test_a_staged_desired_swap_is_promoted_on_the_next_reconcile(tmp_path) -> None:
    """THE regression: resident_persona_swap STAGES a desired tuple and returns;
    only the NEXT boot's reconcile actually activates it. Before the fix,
    reconcile read ONLY active.yaml and a successfully staged swap was silently
    discarded on restart."""
    role = _role()
    default = _persona("casa/tina", "0.1.0")
    override = _persona("casa/gary", "0.2.0")
    instance_dir = InstanceDir(tmp_path / "resident-butler")

    first = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )
    assert first.binding.mode == "image-default"

    override_binding = materialize_override_binding(
        role=role, persona=override, override_source="operator:casa/gary@0.2.0",
    )
    instance_dir.stage_desired(InstanceTuple(
        root="operator:casa/gary@0.2.0", binding=override_binding,
        config_snapshot={}, config_digest=override_binding.effective_config_digest,
    ))
    assert instance_dir.active().binding.mode == "image-default"  # staging alone never activates

    second = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({"casa/gary@0.2.0": override}), instance_dir=instance_dir,
    )
    assert second.binding.mode == "override"
    assert second.binding.persona_id == "casa/gary"
    assert instance_dir.desired() is None       # commit clears desired.yaml
    assert instance_dir.active() == second
    assert (tmp_path / "resident-butler" / "active.prior.yaml").exists()  # rollback target retained


def test_an_invalid_staged_swap_is_rejected_and_active_keeps_running(tmp_path) -> None:
    """A staged desired candidate whose persona blob has since become
    unavailable is discarded with a diagnostic — the resident boots on the
    RETAINED active tuple, never crash-loops."""
    role = _role()
    default = _persona("casa/tina", "0.1.0")
    instance_dir = InstanceDir(tmp_path / "resident-butler")
    first = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )

    missing_binding = materialize_override_binding(
        role=role, persona=_persona("casa/ghost", "0.9.0"), override_source="operator:casa/ghost@0.9.0",
    )
    instance_dir.stage_desired(InstanceTuple(
        root="operator:casa/ghost@0.9.0", binding=missing_binding,
        config_snapshot={}, config_digest=missing_binding.effective_config_digest,
    ))

    second = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}),  # the staged persona is NOT resolvable
        instance_dir=instance_dir,
    )
    assert second == first  # unchanged — boot proceeds on the last-known-good binding
    assert (tmp_path / "resident-butler" / "desired.error.yaml").exists()
    assert instance_dir.desired() is None


def test_a_staged_candidate_identical_to_active_is_a_no_op_and_clears_the_stale_file(tmp_path) -> None:
    """resident_persona_reset staging a return-to-default when already on the
    default must not error or churn — and must not leave a stale desired.yaml."""
    role = _role()
    default = _persona("casa/tina", "0.1.0")
    instance_dir = InstanceDir(tmp_path / "resident-butler")
    first = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )
    from personality_binding import materialize_image_default_binding
    same = materialize_image_default_binding(
        role=role, persona=default, image_default_root="casa/tina@0.1.0",
    )
    instance_dir.stage_desired(InstanceTuple(
        root="casa/tina@0.1.0", binding=same, config_snapshot={}, config_digest=same.effective_config_digest,
    ))
    second = reconcile_resident_binding(
        role=role, image_default_persona_loader=_loaders({"casa/tina@0.1.0": default}),
        override_persona_loader=_loaders({}), instance_dir=instance_dir,
    )
    assert second == first
    assert instance_dir.desired() is None  # the no-op staged file is cleared, not left behind
