"""Personality Phase A, Task 8: prompt compiler order/ceiling/determinism.

The compiler assembles the immutable per-surface resident system prompt from
the canonical role doctrine + bound persona pack, framed by the image-owned
platform-frame and safety-kernel. Section ORDER, the restricted-webhook
persona strip, admission-ceiling enforcement, and byte-for-byte determinism
are the load-bearing invariants proven here.
"""

from __future__ import annotations

import pytest

from persona_pack import PersonaManifest, PersonaPack
from personality_binding import materialize_image_default_binding
from prompt_compiler import compile_prompt_bundle
from role_slot import ResolvedModel, RoleSlot


@pytest.fixture
def role_factory():
    def make():
        resolved = ResolvedModel(
            source="ha_option", effective="haiku",
            sdk_model="claude-haiku-4-5", option="voice_agent_model",
        )
        normalized = {
            "api_version": "casa.role/v1", "id": "resident:butler", "kind": "resident",
            "slot": "butler", "mission": "Control the household.", "model": {
                "source": "ha_option", "option": "voice_agent_model", "default": "haiku",
                "allowed": ["opus", "sonnet", "haiku"],
            },
            "model_resolved": {"effective": "haiku", "sdk_model": "claude-haiku-4-5"},
        }
        return RoleSlot(
            role_id="resident:butler", kind="resident", slot="butler",
            mission="Control the household.", resolved_model=resolved, normalized=normalized,
            doctrine=(
                "# Core doctrine\n\nControl things.\n\n## Text projection\n\nBe brief.\n\n"
                "## Voice projection\n\nBe brief and spoken.\n\n"
                "## Restricted webhook projection\n\nBe plain.\n"
            ),
            checksum="sha256:" + "1" * 64,
        )
    return make


@pytest.fixture
def persona_factory():
    def make():
        return PersonaPack(
            persona_id="casa/tina", version="0.1.0", trait_schema_version=1,
            identity={"display_name": "Tina", "pronouns": {
                "subject": "she", "object": "her", "possessive_adjective": "her",
                "possessive_pronoun": "hers", "reflexive": "herself",
            }},
            relationship_posture="established", archetype="housekeeper",
            traits={"warmth": 3, "formality": 2, "candor": 4, "attunement": 4,
                    "curiosity": 3, "levity": 2, "social_energy": 3, "optimism": 3},
            quirks=(), markdown="# Core\n\nTina keeps the house running.\n\n## Negative space\n\nNever gossips.\n",
            examples=(),
            manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
            checksum="sha256:" + "2" * 64,
        )
    return make


@pytest.fixture
def binding_factory():
    def make(role, persona):
        return materialize_image_default_binding(
            role=role, persona=persona, image_default_root=f"{persona.persona_id}@{persona.version}",
        )
    return make


def test_compiler_order_and_restricted_projection(role_factory, persona_factory, binding_factory) -> None:
    role, persona = role_factory(), persona_factory()
    binding = binding_factory(role, persona)
    bundle = compile_prompt_bundle(
        role=role, persona=persona, binding=binding,
        platform_frame="Platform.\n", safety_kernel="Safety.\n",
    )
    text = bundle.text.system_prompt
    assert text.index("<platform_frame>") < text.index("<role_identity>")
    assert text.index("<role_identity>") < text.index("<persona>")
    assert text.index("<persona>") < text.index("<role_doctrine>")
    assert text.index("<role_doctrine>") < text.index("<safety_kernel>")
    assert text.endswith("</safety_kernel>\n")
    assert "<persona>" not in bundle.restricted_webhook.system_prompt
    assert persona.identity["display_name"] not in bundle.restricted_webhook.system_prompt


def test_binding_digest_mismatch_is_rejected(role_factory, persona_factory, binding_factory) -> None:
    import dataclasses

    role, persona = role_factory(), persona_factory()
    binding = binding_factory(role, persona)
    tampered = dataclasses.replace(binding, binding_digest="sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="binding"):
        compile_prompt_bundle(
            role=role, persona=persona, binding=tampered,
            platform_frame="Platform.\n", safety_kernel="Safety.\n",
        )


def test_recompiling_the_same_inputs_is_byte_identical(role_factory, persona_factory, binding_factory) -> None:
    role, persona = role_factory(), persona_factory()
    binding = binding_factory(role, persona)
    first = compile_prompt_bundle(
        role=role, persona=persona, binding=binding,
        platform_frame="Platform.\n", safety_kernel="Safety.\n",
    )
    second = compile_prompt_bundle(
        role=role, persona=persona, binding=binding,
        platform_frame="Platform.\n", safety_kernel="Safety.\n",
    )
    assert first == second
