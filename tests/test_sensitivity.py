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
