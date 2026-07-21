from pathlib import Path

import pytest

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
