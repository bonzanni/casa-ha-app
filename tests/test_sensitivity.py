# tests/test_sensitivity.py
"""Tier vocabulary + clearance/ceiling helpers (design 2026-06-03 §2.2/§2.4)."""
from __future__ import annotations

import pytest

from sensitivity import (
    TIERS, DEFAULT_TIER, readable_tiers, apply_ceiling, clearance_for_channel,
)

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


def test_unknown_channel_defaults_to_private_clearance():
    assert clearance_for_channel("telegram") == "private"
    assert clearance_for_channel("something-else") == "private"


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
