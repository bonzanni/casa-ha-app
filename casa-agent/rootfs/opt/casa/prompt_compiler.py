# casa-agent/rootfs/opt/casa/prompt_compiler.py
"""Personality Phase A, Task 8: the resident prompt compiler.

Assembles the immutable per-surface (text / voice / restricted_webhook)
system prompt for a persona-bearing resident from three image-owned inputs —
the platform frame, the canonical role doctrine, and the bound persona pack —
plus the image-owned safety kernel, in a FIXED section order:

    <platform_frame> <role_identity> <persona> <role_doctrine> <safety_kernel>

The restricted-webhook surface strips the persona entirely (an untrusted
webhook origin must never see the resident's persona identity). Each surface
enforces an admission ceiling (persona token budget + total token budget) and
is byte-for-byte deterministic given identical inputs, so a recompile of the
same role+persona+binding produces an identical prompt and digest.

The compiled bundle is only returned when the supplied ``BindingRecord``'s
``binding_digest`` recomputes to the digest of the exact role+persona pair the
caller is compiling — a loaded binding that does not match the role/persona it
claims to bind is rejected as tampered/stale.
"""
from __future__ import annotations

from dataclasses import dataclass

from canonical_bytes import canonical_text, checksum_bytes
from markdown_sections import sections, select_markdown_sections
from personality_binding import BindingRecord, compute_binding_digest
from role_slot import RoleSlot
from trait_renderer import RENDERER_VERSION, estimate_tokens_v1, render_v1

# Per-surface (persona_token_ceiling, total_token_ceiling). The persona budget
# bounds how much authored persona prose can dominate the prompt; the total
# budget bounds the assembled projection. voice is tighter than text (a spoken
# turn is latency-bound); restricted_webhook admits no persona at all.
_LIMITS = {"text": (2000, 12000), "voice": (400, 6000), "restricted_webhook": (0, 4000)}


@dataclass(frozen=True, slots=True)
class CompiledProjection:
    system_prompt: str
    digest: str
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class CompiledPromptBundle:
    role_id: str
    resolved_model: str
    text: CompiledProjection
    voice: CompiledProjection
    restricted_webhook: CompiledProjection
    binding_digest: str


def _section(tag: str, body: str) -> str:
    return f"<{tag}>\n{body.rstrip(chr(10))}\n</{tag}>\n"


def _projection(parts: list[tuple[str, str]]) -> CompiledProjection:
    prompt = "\n".join(_section(tag, body).rstrip("\n") for tag, body in parts) + "\n"
    return CompiledProjection(
        system_prompt=prompt, digest=checksum_bytes(prompt.encode("utf-8")),
        estimated_tokens=estimate_tokens_v1(prompt),
    )


def _persona_body(persona, surface: str) -> str:
    if surface == "restricted_webhook":
        return ""
    identity = persona.identity
    pronouns = identity["pronouns"]
    core = select_markdown_sections(persona.markdown, ("Core",))
    body = [
        f"Display name: {identity['display_name']}",
        (f"Pronouns: {pronouns['subject']}/{pronouns['object']}/"
         f"{pronouns['possessive_adjective']}/{pronouns['possessive_pronoun']}/{pronouns['reflexive']}"),
        render_v1(persona.traits, persona.relationship_posture),
        core.rstrip("\n"),
    ]
    if surface == "text":
        body.extend(
            body_part.rstrip("\n") for _, name, body_part in sections(persona.markdown) if name != "Core"
        )
        body.extend(f"Quirk ({q['frequency']}): when {q['context']}, {q['tendency']}." for q in persona.quirks)
        body.extend(
            f"Example user: {e['user']}\nGood: {e['good']}\nBad: {e['bad']}"
            for e in persona.examples if e["surface"] in {"text", "any"}
        )
    else:
        body.extend(f"Quirk ({q['frequency']}): when {q['context']}, {q['tendency']}." for q in persona.quirks[:2])
    return "\n\n".join(v for v in body if v)


def compile_projection_set(
    *, role: RoleSlot, persona, platform_frame: str, safety_kernel: str,
) -> dict[str, CompiledProjection]:
    projections: dict[str, CompiledProjection] = {}
    for surface in ("text", "voice", "restricted_webhook"):
        persona_body = _persona_body(persona, surface)
        doctrine = select_markdown_sections(role.doctrine, {
            "text": ("Core doctrine", "Text projection"),
            "voice": ("Core doctrine", "Voice projection"),
            "restricted_webhook": ("Core doctrine", "Restricted webhook projection"),
        }[surface])
        parts = [
            ("platform_frame", canonical_text(platform_frame)),
            ("role_identity", f"id: {role.role_id}\nkind: {role.kind}\nmission: {role.mission}\n"),
        ]
        if persona_body:
            parts.append(("persona", persona_body))
        parts.extend([("role_doctrine", doctrine), ("safety_kernel", canonical_text(safety_kernel))])
        projection = _projection(parts)
        persona_tokens = estimate_tokens_v1(persona_body)
        persona_limit, total_limit = _LIMITS[surface]
        if persona_tokens > persona_limit or projection.estimated_tokens > total_limit:
            raise ValueError(f"{surface} prompt exceeds admission ceiling for role {role.role_id}")
        projections[surface] = projection
    return projections


def compile_prompt_bundle(
    *, role: RoleSlot, persona, binding: BindingRecord, platform_frame: str, safety_kernel: str,
) -> CompiledPromptBundle:
    projections = compile_projection_set(
        role=role, persona=persona, platform_frame=platform_frame, safety_kernel=safety_kernel,
    )
    expected_digest = compute_binding_digest(
        stable_agent_id=role.role_id, role_checksum=role.checksum,
        persona_id=persona.persona_id, persona_version=persona.version,
        persona_checksum=persona.checksum, compiler_schema_version=RENDERER_VERSION,
        dependency_digests=binding.dependency_digests,
        effective_config_digest=binding.effective_config_digest,
    )
    if (binding.binding_digest != expected_digest or binding.stable_agent_id != role.role_id
            or binding.role_checksum != role.checksum or binding.persona_id != persona.persona_id
            or binding.persona_version != persona.version or binding.persona_checksum != persona.checksum):
        raise ValueError(f"loaded binding for {role.role_id} does not match the compiled role+persona")
    return CompiledPromptBundle(
        role_id=role.role_id, resolved_model=role.resolved_model.effective,
        text=projections["text"], voice=projections["voice"],
        restricted_webhook=projections["restricted_webhook"], binding_digest=binding.binding_digest,
    )


def projection_for(bundle: CompiledPromptBundle, *, channel: str, origin_route: str | None) -> CompiledProjection:
    if origin_route == "webhook_trigger":
        return bundle.restricted_webhook
    if channel == "voice":
        return bundle.voice
    return bundle.text
