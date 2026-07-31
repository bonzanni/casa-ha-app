"""Personality Phase A, Task 8: live-boot integration for the real residents.

Proves Ellen/Tina/Gary each boot with a non-empty compiled prompt bundle from
the REAL shipped image tree, that a personality-identity change refuses a
hot-swap (ReloadError kind=restart_required) while leaving the live bundle
object-identical, and that swap/reset stage a desired tuple that the next boot
reconcile promotes.
"""

from __future__ import annotations

import pytest

_AGENTS = "casa/rootfs/opt/casa/defaults/agents"
_POLICIES = "casa/rootfs/opt/casa/defaults/policies/disclosure.yaml"


def _load(role: str):
    from agent_loader import load_agent_from_dir
    from policies import load_policies

    policies = load_policies(_POLICIES)
    return load_agent_from_dir(f"{_AGENTS}/{role}", policies=policies)


def test_concierge_gary_boots_with_a_real_compiled_binding() -> None:
    cfg = _load("concierge")
    assert cfg.role == "concierge"
    assert cfg.kind == "resident"
    assert cfg.role_id == "resident:concierge"
    assert cfg.role_checksum.startswith("sha256:")
    assert cfg.resolved_model in {"opus", "sonnet", "haiku"}
    assert cfg.persona_pack.persona_id == "casa/gary"
    assert cfg.binding_digest.startswith("sha256:")
    assert cfg.speaker_provenance.display_name == cfg.persona_pack.identity["display_name"]
    assert cfg.compiled_prompt_bundle.text.system_prompt
    assert cfg.compiled_prompt_bundle.voice.system_prompt


@pytest.mark.parametrize("role,persona_id", [
    ("assistant", "casa/ellen"),
    ("butler", "casa/tina"),
])
def test_each_resident_boots_with_its_image_default_persona(role, persona_id) -> None:
    cfg = _load(role)
    assert cfg.kind == "resident"
    assert cfg.persona_pack.persona_id == persona_id
    assert cfg.binding.mode == "image-default"
    assert cfg.compiled_prompt_bundle.text.system_prompt
    # The restricted-webhook projection never carries the persona display name.
    assert cfg.persona_pack.identity["display_name"] not in (
        cfg.compiled_prompt_bundle.restricted_webhook.system_prompt
    )


def test_projection_selects_surface_by_channel_and_route() -> None:
    from prompt_compiler import projection_for

    cfg = _load("concierge")
    bundle = cfg.compiled_prompt_bundle
    assert projection_for(bundle, channel="telegram", origin_route=None) is bundle.text
    assert projection_for(bundle, channel="voice", origin_route=None) is bundle.voice
    assert projection_for(
        bundle, channel="webhook", origin_route="webhook_trigger",
    ) is bundle.restricted_webhook


@pytest.mark.asyncio
async def test_reload_refuses_hot_swap_across_identity_change(tmp_path, monkeypatch) -> None:
    """A resident reload whose new_cfg.role_checksum differs from the live cfg
    raises ReloadError(kind='restart_required') BEFORE any construction, leaving
    the live compiled bundle object-identical.

    Pins INV-CFG-003. Red case demonstrated: neutering reload_agent's _resident_identity_changed guard fails this test.
    """
    from types import SimpleNamespace

    import reload as reload_mod
    from reload import ReloadError, reload_agent

    # Live resident cfg with a fixed identity + a bundle we assert stays put.
    live_bundle = object()
    live_cfg = SimpleNamespace(
        role="concierge", role_checksum="sha256:" + "a" * 64,
        binding_digest="sha256:" + "b" * 64, compiled_prompt_bundle=live_bundle,
    )
    # The would-be reloaded cfg carries a DIFFERENT role_checksum.
    new_cfg = SimpleNamespace(
        role="concierge", role_checksum="sha256:" + "c" * 64,
        binding_digest="sha256:" + "b" * 64, compiled_prompt_bundle=object(),
    )

    resident_dir = tmp_path / "agents" / "concierge"
    resident_dir.mkdir(parents=True)
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "disclosure.yaml").write_text("schema_version: 1\n", encoding="utf-8")

    monkeypatch.setattr(
        "policies.load_policies", lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        "agent_loader.load_agent_from_dir", lambda *a, **k: new_cfg,
    )
    construct_calls = []
    monkeypatch.setattr(
        reload_mod, "_construct_agent",
        lambda *a, **k: construct_calls.append(1),
    )

    runtime = SimpleNamespace(
        config_dir=str(tmp_path), agents_dir=str(tmp_path / "agents"),
        role_configs={"concierge": live_cfg}, agents={},
    )

    with pytest.raises(ReloadError) as excinfo:
        await reload_agent(runtime, role="concierge")
    assert excinfo.value.kind == "restart_required"
    # No construction happened; the live registry is untouched.
    assert construct_calls == []
    assert runtime.role_configs["concierge"] is live_cfg
    assert runtime.role_configs["concierge"].compiled_prompt_bundle is live_bundle


def test_real_concierge_role_stages_a_reset_to_its_own_default(tmp_path) -> None:
    """A swap/reset round-trip that lands a new binding_digest and a return to
    mode == image-default is proven against a role with a broad compat in
    test_reconcile_resident_binding. Here we prove the SHIPPED concierge role
    (slug-pinned to casa/gary) rejects an off-list persona at the compat gate,
    and that staging its own default is a clean image-default no-op — the
    behaviors the resident_persona_reset tool relies on."""
    from pathlib import Path

    from personality_binding import (
        IMAGE_DEFAULT_PERSONA_BY_SLOT,
        InstanceDir,
        InstanceTuple,
        check_persona_requirements,
        materialize_image_default_binding,
        reconcile_resident_binding,
    )
    from persona_pack import load_persona_pack

    cfg = _load("concierge")
    role = cfg.role_slot
    boot_digest = cfg.binding_digest

    gary_dir = Path("casa/rootfs/opt/casa/defaults/personas/casa/gary/0.1.0")
    gary = load_persona_pack(gary_dir / "pack", gary_dir / "manifest.json")
    tina_dir = Path("casa/rootfs/opt/casa/defaults/personas/casa/tina/0.1.0")
    tina = load_persona_pack(tina_dir / "pack", tina_dir / "manifest.json")

    # The shipped concierge role only accepts casa/gary — an off-list persona is
    # rejected at the compat gate (what resident_persona_swap enforces).
    check_persona_requirements(role.normalized, gary)  # accepted
    with pytest.raises(ValueError, match="persona_requirements"):
        check_persona_requirements(role.normalized, tina)

    # Staging concierge's own image default is a clean no-op that clears desired.
    instance_dir = InstanceDir(tmp_path / "resident-concierge")
    first = reconcile_resident_binding(
        role=role, image_default_persona_loader=lambda _r: gary,
        override_persona_loader=lambda _r: gary, instance_dir=instance_dir,
    )
    assert first.binding.mode == "image-default"
    assert first.binding.binding_digest == boot_digest

    default_ref = IMAGE_DEFAULT_PERSONA_BY_SLOT["concierge"]
    reset_binding = materialize_image_default_binding(
        role=role, persona=gary, image_default_root=default_ref,
    )
    instance_dir.stage_desired(InstanceTuple(
        root=default_ref, binding=reset_binding,
        config_snapshot={}, config_digest=reset_binding.effective_config_digest,
    ))
    reset = reconcile_resident_binding(
        role=role, image_default_persona_loader=lambda _r: gary,
        override_persona_loader=lambda _r: gary, instance_dir=instance_dir,
    )
    assert reset == first
    assert reset.binding.mode == "image-default"
    assert instance_dir.desired() is None


def test_missing_override_persona_blob_at_reload_raises_load_error(tmp_path) -> None:
    """Fix 2 regression: reconcile_resident_binding can return a RETAINED
    last-known-good OVERRIDE tuple without itself raising -- e.g. when nothing
    is staged and the persona blob backing the current active override has
    since vanished from disk, reconcile's own persona-load attempt fails,
    is caught internally, and (since an active tuple exists) the retained
    active is returned unchanged. agent_loader._activate_resident_binding then
    reloads that SAME now-missing persona a second time, outside reconcile's
    try/except ValueError -> LoadError translation. Before the fix that second
    load escaped as a raw ValueError; it must be a LoadError instead.

    Pins INV-PERS-003. Red case demonstrated: skipping _activate_resident_binding in resident loading fails this test.
    """
    from persona_pack import PersonaManifest, PersonaPack
    from personality_binding import InstanceDir, InstanceTuple, materialize_override_binding
    from agent_loader import LoadError, load_agent_from_dir
    from policies import load_policies

    cfg = _load("concierge")
    role = cfg.role_slot

    ghost = PersonaPack(
        persona_id="casa/gary", version="9.9.9", trait_schema_version=1,
        identity={"display_name": "Ghost Gary", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="concierge",
        traits={"warmth": 3, "formality": 2, "candor": 4, "attunement": 4,
                "curiosity": 3, "levity": 2, "social_energy": 3, "optimism": 3},
        quirks=(), markdown="# Core\n\nGhost.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "9" * 64),
        checksum="sha256:" + "8" * 64,
    )
    binding = materialize_override_binding(
        role=role, persona=ghost, override_source="operator:casa/gary@9.9.9",
    )
    bindings_root = tmp_path / "bindings-root"
    instance_dir = InstanceDir(bindings_root / "resident-concierge")
    instance_dir.stage_desired(InstanceTuple(
        root="operator:casa/gary@9.9.9", binding=binding,
        config_snapshot={}, config_digest=binding.effective_config_digest,
    ))
    instance_dir.commit_desired_to_active()
    assert instance_dir.active().binding.persona_version == "9.9.9"  # blob never installed

    policies = load_policies(_POLICIES)
    with pytest.raises(LoadError):
        load_agent_from_dir(
            f"{_AGENTS}/concierge", policies=policies,
            bindings_dir=str(bindings_root),
        )


def test_a_staged_override_with_a_wrong_checksum_is_discarded_and_boot_retains_default(
        tmp_path) -> None:
    """#339: end-to-end through the REAL loader. A stale/tampered staged
    desired tuple pins a persona_checksum that does not match the bytes on
    disk for casa/gary@0.1.0. Reconcile must refuse it (checksum pin),
    discard the candidate, and the concierge must boot on its image-default
    binding — before the fix the loader committed a rematerialized binding
    for whatever bytes were present."""
    import dataclasses
    from pathlib import Path

    from personality_binding import InstanceDir, InstanceTuple, materialize_override_binding
    from persona_pack import load_persona_pack
    from agent_loader import load_agent_from_dir
    from policies import load_policies

    cfg = _load("concierge")
    role = cfg.role_slot
    gary_dir = Path("casa/rootfs/opt/casa/defaults/personas/casa/gary/0.1.0")
    gary = load_persona_pack(gary_dir / "pack", gary_dir / "manifest.json")
    tampered = dataclasses.replace(gary, checksum="sha256:" + "f" * 64)

    binding = materialize_override_binding(
        role=role, persona=tampered, override_source="operator:casa/gary@0.1.0",
    )
    bindings_root = tmp_path / "bindings-root"
    policies = load_policies(_POLICIES)
    # First boot establishes the image-default active tuple (the
    # last-known-good the second boot must retain).
    load_agent_from_dir(
        f"{_AGENTS}/concierge", policies=policies, bindings_dir=str(bindings_root),
    )
    instance_dir = InstanceDir(bindings_root / "resident-concierge")
    instance_dir.stage_desired(InstanceTuple(
        root="operator:casa/gary@0.1.0", binding=binding,
        config_snapshot={}, config_digest=binding.effective_config_digest,
    ))

    loaded = load_agent_from_dir(
        f"{_AGENTS}/concierge", policies=policies, bindings_dir=str(bindings_root),
    )
    assert loaded.binding.mode == "image-default"
    assert loaded.binding.persona_checksum == gary.checksum
    assert (bindings_root / "resident-concierge" / "desired.error.yaml").exists()


def test_loader_wires_a_candidate_validator_that_enforces_compile_ceilings(
        tmp_path, monkeypatch) -> None:
    """#339 (high): _activate_resident_binding must hand reconcile a
    candidate_validator that runs the REAL compile admission — so an
    over-ceiling persona fails as a *candidate* (discarded, active retained)
    instead of after commit (boot-fatal forever). Captures the validator the
    loader passes and proves it rejects an over-ceiling persona for the
    shipped concierge role."""
    from pathlib import Path

    import personality_binding
    from persona_pack import load_persona_pack, PersonaManifest, PersonaPack

    captured: dict = {}
    real = personality_binding.reconcile_resident_binding

    def spy(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(personality_binding, "reconcile_resident_binding", spy)
    monkeypatch.setenv("CASA_BINDINGS_DIR", str(tmp_path / "bindings-root"))
    cfg = _load("concierge")
    validator = captured.get("candidate_validator")
    assert validator is not None, (
        "loader must pass a compile-admission candidate_validator to reconcile"
    )

    gary_dir = Path("casa/rootfs/opt/casa/defaults/personas/casa/gary/0.1.0")
    gary = load_persona_pack(gary_dir / "pack", gary_dir / "manifest.json")
    # The shipped persona compiles — the validator accepts it.
    ok_binding = personality_binding.materialize_override_binding(
        role=cfg.role_slot, persona=gary, override_source="operator:casa/gary@0.1.0",
    )
    validator(gary, ok_binding)

    # An over-ceiling variant (same structure, bloated core prose) is refused.
    import dataclasses
    huge = dataclasses.replace(
        gary,
        markdown="# Core\n\n" + ("Verbose prose. " * 4000) + "\n",
    )
    huge_binding = personality_binding.materialize_override_binding(
        role=cfg.role_slot, persona=huge, override_source="operator:casa/gary@0.1.0",
    )
    with pytest.raises(ValueError, match="ceiling"):
        validator(huge, huge_binding)


@pytest.mark.asyncio
async def test_persona_swap_tool_structured_errors(monkeypatch) -> None:
    """The persona-swap tool returns structured errors before any staging:
    an unknown slot is invalid_role; an unbound runtime is runtime_unavailable."""
    import json

    from tools import resident_persona_swap

    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", None, raising=False)

    def _payload(result: dict) -> dict:
        return json.loads(result["content"][0]["text"])

    bad_role = await resident_persona_swap.handler(
        {"role": "resident:nope", "persona_ref": "casa/gary@0.1.0"},
    )
    assert _payload(bad_role)["kind"] == "invalid_role"

    no_runtime = await resident_persona_swap.handler(
        {"role": "resident:concierge", "persona_ref": "casa/gary@0.1.0"},
    )
    assert _payload(no_runtime)["kind"] == "runtime_unavailable"


def test_changed_bytes_under_an_active_override_fail_the_load_as_load_error(
        tmp_path) -> None:
    """Terra review (#339): with an ACTIVE override whose on-disk bytes
    changed under the pinned version, reconcile refuses to rematerialize
    (checksum pin) and retains the active tuple — and the loader's compile
    integrity check then REFUSES the changed pack against the retained
    binding. The altered bytes are never served; the resident fails to load
    with a typed LoadError (boot-fatal per INV-PERS-003)."""
    import dataclasses
    from pathlib import Path

    from personality_binding import InstanceDir, InstanceTuple, materialize_override_binding
    from persona_pack import load_persona_pack
    from agent_loader import LoadError, load_agent_from_dir
    from policies import load_policies

    cfg = _load("concierge")
    role = cfg.role_slot
    gary_dir = Path("casa/rootfs/opt/casa/defaults/personas/casa/gary/0.1.0")
    gary = load_persona_pack(gary_dir / "pack", gary_dir / "manifest.json")

    # The ACTIVE binding pins a checksum that does not match the bytes the
    # loader will find on disk — the "restore changed the bytes under a
    # pinned version" state.
    approved_elsewhere = dataclasses.replace(gary, checksum="sha256:" + "e" * 64)
    binding = materialize_override_binding(
        role=role, persona=approved_elsewhere, override_source="operator:casa/gary@0.1.0",
    )
    bindings_root = tmp_path / "bindings-root"
    instance_dir = InstanceDir(bindings_root / "resident-concierge")
    instance_dir.stage_desired(InstanceTuple(
        root="operator:casa/gary@0.1.0", binding=binding,
        config_snapshot={}, config_digest=binding.effective_config_digest,
    ))
    instance_dir.commit_desired_to_active()

    policies = load_policies(_POLICIES)
    with pytest.raises(LoadError):
        load_agent_from_dir(
            f"{_AGENTS}/concierge", policies=policies, bindings_dir=str(bindings_root),
        )
    # The altered on-disk pack was never accepted into the binding.
    assert instance_dir.active().binding.persona_checksum == "sha256:" + "e" * 64
