# casa-agent/rootfs/opt/casa/recall_renderer.py
"""Attributed recall renderer (personality Task 11).

Turns typed :class:`RecallHit`s into a digest whose every line carries an
HONEST attribution derived from the hit's OWN decoded provenance tag — the
identity recorded WHEN the memory was written, never a live lookup against the
currently-installed persona packs. A persona retired or replaced since the
memory was written is therefore still attributed by its historical identity.

Distinct from ``semantic_memory.render_recall`` (the legacy flat
``"- {text}"`` bullet renderer used by the untyped ``recall()`` path) — this
module is the NEW typed renderer for ``recall_items()`` and must never be
conflated with it. Reserved ``casa-source-`` tags and bare tier tokens never
appear in the output (they are already stripped from ``application_tags`` by
the decode, and this renderer never emits ``application_tags`` at all).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from personality_types import RecallHit, SpeakerProvenance
from trait_renderer import estimate_tokens_v1

_RANK = {"public": 0, "friends": 1, "family": 2, "private": 3}

Surface = Literal["text", "voice", "restricted_webhook"]


@dataclass(frozen=True, slots=True)
class ProvenanceView:
    """The clearance/surface-gated projection of a hit's provenance — the only
    identity fields that may be spoken at this clearance on this surface."""
    speaker_kind: str
    display_name: str | None = None
    role_id: str | None = None
    persona_id: str | None = None
    persona_version: str | None = None


def provenance_view(
    value: SpeakerProvenance | None, *, clearance: str, surface: Surface,
) -> ProvenanceView | None:
    """Gate a hit's recorded provenance by clearance + surface.

    Reads identity STRAIGHT from the hit's own decoded provenance — never a
    live ``CasaRuntime.persona_packs`` lookup — so a retired/replaced persona
    is still attributed by its historical identity. A restricted-webhook
    surface pins the effective rank to 0 (public), so it never names a person
    regardless of the turn's clearance."""
    if value is None:
        return None
    rank = 0 if surface == "restricted_webhook" else _RANK.get(clearance, 0)
    display_name = value.display_name if rank >= 1 else None
    role_id = value.role_id if rank >= 1 else None
    persona_id = value.persona_id if rank >= 3 else None
    persona_version = value.persona_version if rank >= 3 else None
    if value.speaker_kind == "user" and value.user_id is None:
        # Anonymous / shared-secret users never become named people.
        display_name = None
    return ProvenanceView(
        speaker_kind=value.speaker_kind, display_name=display_name, role_id=role_id,
        persona_id=persona_id, persona_version=persona_version,
    )


def render_recall(
    hits: Sequence[RecallHit], *, current_speaker: SpeakerProvenance,
    surface: Surface, clearance: str, token_budget: int,
) -> str:
    """Render ``hits`` into an attributed digest, stopping once ``token_budget``
    would be exceeded. ``current_speaker`` is the identity of the agent doing
    the recall (reserved for future first-person/third-person distinctions);
    attribution itself is driven entirely by each hit's recorded provenance."""
    lines: list[str] = []
    for hit in hits:
        entry: list[str] = []
        allowed = provenance_view(hit.provenance, clearance=clearance, surface=surface)
        if allowed is None:
            entry.extend([
                f"- A prior source recorded: {hit.text}",
                "  [source unavailable; do not treat this as first-person recollection]",
            ])
        elif allowed.speaker_kind == "user":
            speaker = allowed.display_name or "A prior user"
            entry.append(f"- {speaker} said: {hit.text}")
        elif allowed.display_name and allowed.role_id:
            source = allowed.role_id
            if allowed.persona_id and allowed.persona_version:
                source += f", {allowed.persona_id}@{allowed.persona_version}"
            entry.extend([
                f"- {allowed.display_name} previously said: {hit.text}",
                f"  [source: {source}]",
            ])
        else:
            entry.extend([
                f"- A prior Casa model output said: {hit.text}",
                "  [source identity unavailable at this clearance; treat as a prior assertion]",
            ])
        candidate = "\n".join([*lines, *entry])
        if estimate_tokens_v1(candidate) > token_budget:
            break
        lines.extend(entry)
    return "\n".join(lines)
