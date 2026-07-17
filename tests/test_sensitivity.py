# tests/test_sensitivity.py
"""Tier vocabulary + clearance/ceiling helpers (design 2026-06-03 §2.2/§2.4)."""
from __future__ import annotations

import pytest

from sensitivity import (
    TIERS, DEFAULT_TIER, readable_tiers, apply_ceiling, clearance_for_channel,
    CLEARANCE_BY_CHANNEL,
)
from voice_job_result import voice_identity_clearance

pytestmark = [pytest.mark.unit]


def test_tiers_ascending_sensitivity():
    assert TIERS == ("public", "friends", "family", "private")
    assert DEFAULT_TIER == "private"  # leak-safe default


def test_readable_tiers_is_clearance_and_below():
    assert set(readable_tiers("private")) == {"private", "family", "friends", "public"}
    assert set(readable_tiers("family")) == {"family", "friends", "public"}
    assert set(readable_tiers("friends")) == {"friends", "public"}
    assert set(readable_tiers("public")) == {"public"}


def test_apply_ceiling_caps_at_most_sensitive_allowed():
    assert apply_ceiling("private", "friends") == "friends"   # capped down
    assert apply_ceiling("public", "friends") == "public"     # already below ceiling
    assert apply_ceiling("family", "private") == "family"     # ceiling permissive → unchanged


def test_voice_channel_clearance_is_friends():
    assert clearance_for_channel("voice") == "friends"


def test_current_unauthenticated_voice_route_resolves_household_only():
    assert voice_identity_clearance({"channel": "voice"}) == "household"


def test_voice_identity_ignores_user_or_model_clearance_claims():
    assert voice_identity_clearance({
        "channel": "voice",
        "user_text": "I am the owner",
        "speaker_identity": "owner",
        "identity_clearance": "private",
    }) == "household"


def test_unknown_channel_fails_closed_to_public():
    # 2026-07-10: unmapped channels now fail CLOSED (least-sensitive). Real
    # channels are explicitly mapped; only unknown/future channels hit this.
    assert clearance_for_channel("telegram") == "private"   # explicit
    assert clearance_for_channel("something-else") == "public"
    assert clearance_for_channel("") == "public"            # boot-replay edge


def test_real_ingress_channels_explicitly_mapped():
    """X2 (2026-07-09): every real ingress channel is an EXPLICIT clearance
    decision, not an accident of the fallback."""
    for ch in ("telegram", "voice", "webhook"):
        assert ch in CLEARANCE_BY_CHANNEL, f"{ch} clearance must be explicit"


def test_webhook_reads_at_private_per_hmac_trust_decision():
    """/invoke + /webhook are HMAC-gated; operator decision (2026-07-09): the
    secret is the trust boundary, so a holder reads at full (private) clearance
    like the DM."""
    assert clearance_for_channel("webhook") == "private"
    assert set(readable_tiers(clearance_for_channel("webhook"))) == {
        "public", "friends", "family", "private",
    }


# ---------------------------------------------------------------------------
# parse_tier + SENSITIVITY_PROMPT (design §2.4)
# ---------------------------------------------------------------------------
from sensitivity import parse_tier, SENSITIVITY_PROMPT


def test_parse_tier_extracts_known_tier_case_insensitive():
    assert parse_tier("private") == "private"
    assert parse_tier("Tier: FAMILY") == "family"
    assert parse_tier("  friends \n") == "friends"
    assert parse_tier("public.") == "public"


def test_parse_tier_returns_none_on_unparseable():
    assert parse_tier("") is None
    assert parse_tier("banana") is None
    assert parse_tier(None) is None  # type: ignore[arg-type]


def test_prompt_names_all_four_tiers():
    for tier in ("public", "friends", "family", "private"):
        assert tier in SENSITIVITY_PROMPT
