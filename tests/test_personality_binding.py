from pathlib import Path

import pytest
import yaml

from personality_binding import (
    EMPTY_CONFIG_DIGEST,
    BindingRecord,
    InstanceDir,
    InstanceTuple,
    compute_binding_digest,
    compute_effective_config_digest,
)
from trait_renderer import RENDERER_VERSION


def _digest_inputs(**overrides) -> dict:
    base = {
        "stable_agent_id": "resident:butler",
        "role_checksum": "sha256:" + "1" * 64,
        "persona_id": "casa/tina",
        "persona_version": "0.1.0",
        "persona_checksum": "sha256:" + "2" * 64,
        "compiler_schema_version": RENDERER_VERSION,
        "dependency_digests": (),
        "effective_config_digest": EMPTY_CONFIG_DIGEST,
    }
    base.update(overrides)
    return base


def test_digest_changes_for_every_normative_input() -> None:
    baseline = compute_binding_digest(**_digest_inputs())
    for key, value in {
        "stable_agent_id": "resident:concierge",
        "role_checksum": "sha256:" + "9" * 64,
        "persona_id": "casa/gary",
        "persona_version": "0.2.0",
        "persona_checksum": "sha256:" + "8" * 64,
        "compiler_schema_version": RENDERER_VERSION + "-changed",
        "dependency_digests": ("sha256:" + "7" * 64,),
        "effective_config_digest": "sha256:" + "6" * 64,
    }.items():
        assert compute_binding_digest(**_digest_inputs(**{key: value})) != baseline


def test_digest_input_set_matches_the_normative_eight_fields() -> None:
    assert set(_digest_inputs()) == {
        "stable_agent_id", "role_checksum", "persona_id", "persona_version",
        "persona_checksum", "compiler_schema_version", "dependency_digests",
        "effective_config_digest",
    }


def test_dependency_digests_are_order_independent() -> None:
    a = compute_binding_digest(**_digest_inputs(
        dependency_digests=("sha256:" + "1" * 64, "sha256:" + "2" * 64),
    ))
    b = compute_binding_digest(**_digest_inputs(
        dependency_digests=("sha256:" + "2" * 64, "sha256:" + "1" * 64),
    ))
    assert a == b


def test_empty_config_digest_is_stable_and_deterministic() -> None:
    assert EMPTY_CONFIG_DIGEST == compute_effective_config_digest({})
    assert EMPTY_CONFIG_DIGEST.startswith("sha256:")


def _binding(**overrides) -> BindingRecord:
    from personality_binding import compute_binding_digest as digest_fn
    fields = _digest_inputs(**{k: v for k, v in overrides.items() if k in _digest_inputs()})
    digest = digest_fn(**fields)
    return BindingRecord(
        **fields, mode=overrides.get("mode", "image-default"), binding_digest=digest,
        image_default_root=overrides.get("image_default_root", "casa/tina@0.1.0"),
        component_root=overrides.get("component_root"),
        override_source=overrides.get("override_source"),
    )


def _tuple(binding: BindingRecord) -> InstanceTuple:
    return InstanceTuple(
        root=binding.image_default_root or binding.override_source or "",
        binding=binding, config_snapshot={}, config_digest=binding.effective_config_digest,
    )


# --- InstanceDir: defect #4 regression coverage -----------------------------


def test_fresh_instance_dir_has_no_active_or_desired(tmp_path: Path) -> None:
    d = InstanceDir(tmp_path / "resident-butler")
    assert d.active() is None
    assert d.desired() is None


def test_stage_desired_does_not_touch_active(tmp_path: Path) -> None:
    d = InstanceDir(tmp_path / "resident-butler")
    original = _tuple(_binding())
    d.stage_desired(original)
    assert d.active() is None  # staging alone never activates anything
    assert d.desired() == original


def test_commit_moves_desired_to_active_and_retains_prior(tmp_path: Path) -> None:
    d = InstanceDir(tmp_path / "resident-butler")
    first = _tuple(_binding(persona_version="0.1.0"))
    d.stage_desired(first)
    committed_first = d.commit_desired_to_active()
    assert committed_first == first
    assert d.active() == first
    assert d.desired() is None

    second = _tuple(_binding(persona_version="0.2.0"))
    d.stage_desired(second)
    d.commit_desired_to_active()
    assert d.active() == second
    prior_path = tmp_path / "resident-butler" / "active.prior.yaml"
    assert prior_path.exists()
    from personality_binding import load_instance_tuple
    assert load_instance_tuple(prior_path) == first  # rollback target retained
    assert (prior_path.stat().st_mode & 0o777) == 0o600  # defect #2: same lockdown as siblings


def test_commit_is_crash_retry_idempotent_and_preserves_true_prior(tmp_path: Path) -> None:
    """Regression for defect #1: a crash AFTER active.yaml is rewritten to the
    new candidate but BEFORE desired.yaml is unlinked must be a safe no-op
    retry — it must NOT re-rotate the (now-identical) active into prior and
    clobber the true rollback target."""
    from personality_binding import load_instance_tuple

    d = InstanceDir(tmp_path / "resident-butler")
    first = _tuple(_binding(persona_version="0.1.0"))
    d.stage_desired(first)
    d.commit_desired_to_active()

    second = _tuple(_binding(persona_version="0.2.0"))
    d.stage_desired(second)
    d.commit_desired_to_active()

    prior_path = tmp_path / "resident-butler" / "active.prior.yaml"
    assert load_instance_tuple(prior_path) == first

    # Simulate the interrupted-retry crash window: desired.yaml still holds
    # the very tuple that is already active (i.e. the commit ran to
    # completion on active.yaml but the process died before the final
    # desired.yaml unlink, and now it retries).
    d.stage_desired(second)
    d.commit_desired_to_active()

    assert load_instance_tuple(prior_path) == first  # true prior MUST survive the retry
    assert d.active() == second
    assert d.desired() is None


def test_tampered_nested_binding_missing_field_raises_value_error(tmp_path: Path) -> None:
    """Regression for defect #3: a tampered instance tuple whose nested
    binding drops a required field must raise a path-prefixed ValueError,
    not a bare KeyError."""
    d = InstanceDir(tmp_path / "resident-butler")
    d.stage_desired(_tuple(_binding()))
    d.commit_desired_to_active()
    active_path = tmp_path / "resident-butler" / "active.yaml"
    raw = yaml.safe_load(active_path.read_text(encoding="utf-8"))
    del raw["binding"]["persona_checksum"]
    active_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match=str(active_path)):
        d.active()


def test_discard_desired_leaves_active_untouched(tmp_path: Path) -> None:
    d = InstanceDir(tmp_path / "resident-butler")
    d.stage_desired(_tuple(_binding(persona_version="0.1.0")))
    d.commit_desired_to_active()
    active_before = d.active()

    d.stage_desired(_tuple(_binding(persona_version="0.2.0")))
    d.discard_desired(reason="persona blob unavailable")
    assert d.active() == active_before  # unchanged
    assert d.desired() is None  # no longer readable as a valid desired candidate
    assert (tmp_path / "resident-butler" / "desired.error.yaml").exists()


def test_commit_with_nothing_staged_raises(tmp_path: Path) -> None:
    d = InstanceDir(tmp_path / "resident-butler")
    with pytest.raises(ValueError, match="desired"):
        d.commit_desired_to_active()


def test_tampered_active_file_is_rejected_on_load(tmp_path: Path) -> None:
    d = InstanceDir(tmp_path / "resident-butler")
    d.stage_desired(_tuple(_binding()))
    d.commit_desired_to_active()
    active_path = tmp_path / "resident-butler" / "active.yaml"
    text = active_path.read_text(encoding="utf-8")
    active_path.write_text(text.replace("0.1.0", "9.9.9"), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        d.active()


def _specialist_role() -> "RoleSlot":
    from role_slot import RoleSlot, ResolvedModel
    return RoleSlot(
        role_id="specialist:mtg", kind="specialist", slot="mtg", mission="x",
        resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                      sdk_model="claude-sonnet-4-6", option=None),
        normalized={}, doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
    )


def _judge_persona() -> "PersonaPack":
    from persona_pack import PersonaPack, PersonaManifest
    return PersonaPack(
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


def test_materialize_component_default_binding_is_specialist_only() -> None:
    import pytest
    from personality_binding import materialize_component_default_binding
    from role_slot import RoleSlot, ResolvedModel

    resident_role = RoleSlot(
        role_id="resident:butler", kind="resident", slot="butler", mission="x",
        resolved_model=ResolvedModel(source="fixed", effective="haiku",
                                      sdk_model="claude-haiku-4-5", option=None),
        normalized={}, doctrine="Doctrine.\n", checksum="sha256:" + "1" * 64,
    )
    with pytest.raises(ValueError, match="specialist-only"):
        materialize_component_default_binding(
            role=resident_role, persona=_judge_persona(), component_root="casa-test/mtg@0.1.0",
        )


def test_materialize_component_default_binding_sets_mode_and_component_root() -> None:
    from personality_binding import materialize_component_default_binding

    binding = materialize_component_default_binding(
        role=_specialist_role(), persona=_judge_persona(), component_root="casa-test/mtg@0.1.0",
    )
    assert binding.mode == "component-default"
    assert binding.component_root == "casa-test/mtg@0.1.0"
    assert binding.image_default_root is None
    assert binding.override_source is None


def test_materialize_override_binding_default_args_produce_the_pre_n1c_digest() -> None:
    """Task N1c extends materialize_override_binding with two new optional
    kwargs (dependency_digests, effective_config_digest) so upgrade_specialist/
    rollback_specialist can preserve an override-bound specialist's persona
    pin while still capturing the new component's dependency closure. This
    pins the additive-only guarantee: a call with NO new kwargs — exactly
    the shape every existing caller (reconcile_resident_binding,
    tools.py's resident_persona_swap) uses — must produce the IDENTICAL
    binding_digest a hand-built compute_binding_digest call (with the same
    implicit defaults _build already used pre-N1c: dependency_digests=(),
    effective_config_digest=EMPTY_CONFIG_DIGEST) produces."""
    from personality_binding import compute_binding_digest, materialize_override_binding

    role = _specialist_role()
    persona = _judge_persona()

    binding = materialize_override_binding(
        role=role, persona=persona, override_source="operator:casa/judge@0.1.0",
    )
    expected_digest = compute_binding_digest(
        stable_agent_id=role.role_id, role_checksum=role.checksum,
        persona_id=persona.persona_id, persona_version=persona.version,
        persona_checksum=persona.checksum, compiler_schema_version=RENDERER_VERSION,
        dependency_digests=(), effective_config_digest=EMPTY_CONFIG_DIGEST,
    )
    assert binding.binding_digest == expected_digest
    assert binding.mode == "override"
    assert binding.override_source == "operator:casa/judge@0.1.0"
    assert binding.dependency_digests == ()
    assert binding.effective_config_digest == EMPTY_CONFIG_DIGEST
