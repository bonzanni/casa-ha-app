"""Personality Task 11: the attributed recall renderer.

`recall_renderer.render_recall` turns typed `RecallHit`s into an attributed
digest, gating provenance fields by clearance/surface via `provenance_view`.
Attribution is read from the HIT's OWN decoded provenance tag (recorded when
the memory was written), never a live persona lookup — so a retired/replaced
persona is still attributed by its historical identity.
"""
from __future__ import annotations

import pytest

from personality_types import RecallHit, SpeakerProvenance

pytestmark = [pytest.mark.unit]


def _resident(
    *, display_name: str = "Tina", role_id: str = "resident:butler",
    persona_id: str = "casa.personas/tina", persona_version: str = "1.0.0",
) -> SpeakerProvenance:
    return SpeakerProvenance(
        speaker_kind="resident", role_id=role_id, persona_id=persona_id,
        persona_version=persona_version, display_name=display_name,
        binding_digest="sha256:" + "5" * 64,
    )


def _hit(text: str, provenance: SpeakerProvenance | None, *, sensitivity="friends",
         application_tags: tuple[str, ...] = ()) -> RecallHit:
    return RecallHit(
        text=text, memory_type="world", sensitivity=sensitivity,
        application_tags=application_tags, provenance=provenance, backend_id="b1",
        document_id=None, chunk_id=None, source_fact_ids=None, metadata=None,
        context=None, score=None,
    )


@pytest.fixture
def current_tina() -> SpeakerProvenance:
    return _resident()


@pytest.fixture
def tina_hit() -> SpeakerProvenance:
    return _hit("the thermostat is set to 20C", _resident())


@pytest.fixture
def retired_persona_hit() -> RecallHit:
    # A persona (Vera) that is NOT the currently-installed one — its identity
    # survives on the hit's own recorded provenance tag.
    return _hit(
        "the alarm code is 1234",
        _resident(display_name="Vera", persona_id="casa.personas/vera",
                  persona_version="9.9.9"),
        sensitivity="private",
    )


# ---------------------------------------------------------------------------
# Brief Step 1 skeletons (verbatim behaviour)
# ---------------------------------------------------------------------------


def test_same_persona_hit_remains_attributed(current_tina, tina_hit) -> None:
    from recall_renderer import render_recall

    rendered = render_recall(
        (tina_hit,), current_speaker=current_tina, surface="text",
        clearance="friends", token_budget=500,
    )
    assert "- Tina previously said:" in rendered
    assert "I remember" not in rendered


def test_persona_no_longer_installed_still_attributes_from_the_recorded_tag(
    current_tina, retired_persona_hit,
) -> None:
    """A hit carries a decoded provenance tag recorded WHEN it was written — the
    renderer attributes it by that recorded persona_id/version even if no
    currently-installed binding uses that exact persona version any more."""
    from recall_renderer import render_recall

    rendered = render_recall(
        (retired_persona_hit,), current_speaker=current_tina, surface="text",
        clearance="private", token_budget=500,
    )
    assert retired_persona_hit.provenance.display_name in rendered
    assert retired_persona_hit.provenance.persona_version in rendered


# ---------------------------------------------------------------------------
# Clearance / surface gating
# ---------------------------------------------------------------------------


def test_friends_clearance_hides_persona_version(current_tina, tina_hit) -> None:
    from recall_renderer import render_recall

    rendered = render_recall(
        (tina_hit,), current_speaker=current_tina, surface="text",
        clearance="friends", token_budget=500,
    )
    assert "resident:butler" in rendered          # role visible at friends
    assert "casa.personas/tina" not in rendered   # persona only at private
    assert "@1.0.0" not in rendered


def test_private_clearance_reveals_persona(current_tina, tina_hit) -> None:
    from recall_renderer import render_recall

    rendered = render_recall(
        (tina_hit,), current_speaker=current_tina, surface="text",
        clearance="private", token_budget=500,
    )
    assert "[source: resident:butler, casa.personas/tina@1.0.0]" in rendered


def test_public_clearance_strips_all_identity(current_tina, tina_hit) -> None:
    from recall_renderer import render_recall

    rendered = render_recall(
        (tina_hit,), current_speaker=current_tina, surface="text",
        clearance="public", token_budget=500,
    )
    assert "Tina" not in rendered
    assert "resident:butler" not in rendered
    assert "the thermostat is set to 20C" in rendered


def test_restricted_webhook_surface_strips_identity_even_at_high_clearance(
    current_tina, tina_hit,
) -> None:
    """A restricted-webhook turn never names people, regardless of clearance."""
    from recall_renderer import render_recall

    rendered = render_recall(
        (tina_hit,), current_speaker=current_tina, surface="restricted_webhook",
        clearance="private", token_budget=500,
    )
    assert "Tina" not in rendered
    assert "resident:butler" not in rendered
    assert "the thermostat is set to 20C" in rendered


def test_user_hit_named_only_when_identified(current_tina) -> None:
    from recall_renderer import render_recall

    named = _hit(
        "I prefer oat milk",
        SpeakerProvenance(speaker_kind="user", user_peer="telegram_1",
                          user_id="u1", display_name="Nicola"),
    )
    rendered = render_recall(
        (named,), current_speaker=current_tina, surface="text",
        clearance="private", token_budget=500,
    )
    assert "Nicola said: I prefer oat milk" in rendered

    anon = _hit(
        "someone said hi",
        SpeakerProvenance(speaker_kind="user", user_peer="voice_speaker",
                          user_id=None, display_name=None),
    )
    rendered_anon = render_recall(
        (anon,), current_speaker=current_tina, surface="text",
        clearance="private", token_budget=500,
    )
    assert "A prior user said:" in rendered_anon


def test_missing_provenance_falls_back_to_neutral_attribution(current_tina) -> None:
    from recall_renderer import render_recall

    hit = _hit("a fact of unknown origin", None)
    rendered = render_recall(
        (hit,), current_speaker=current_tina, surface="text",
        clearance="private", token_budget=500,
    )
    assert "A prior source recorded: a fact of unknown origin" in rendered
    assert "source unavailable" in rendered


def test_token_budget_truncates_hits(current_tina) -> None:
    from recall_renderer import render_recall

    hits = tuple(_hit(f"fact number {i} " + "x" * 40, None) for i in range(20))
    rendered = render_recall(
        hits, current_speaker=current_tina, surface="text",
        clearance="private", token_budget=20,
    )
    # Budget is small, so not every one of the 20 hits fits.
    assert rendered.count("A prior source recorded:") < 20


def test_raw_tags_never_escape_the_render(current_tina) -> None:
    """Step 8: reserved source tags and bare tier tokens never appear in the
    rendered string. application_tags are never emitted at all."""
    from recall_renderer import render_recall

    hit = _hit(
        "a plain fact", _resident(),
        application_tags=("house",),
    )
    rendered = render_recall(
        (hit,), current_speaker=current_tina, surface="text",
        clearance="private", token_budget=500,
    )
    assert "casa-source-" not in rendered
    for token in ("public", "friends", "family", "private"):
        assert token not in rendered
    assert "house" not in rendered  # application_tags are not rendered
