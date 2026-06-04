# tests/test_agent_read_tiers.py
"""Read path uses tier clearance, not domain scopes: tags + overlay gating."""
import pytest

import agent as agent_mod

pytestmark = [pytest.mark.unit]


def test_recall_tags_for_voice_are_public_and_friends():
    assert sorted(agent_mod._recall_tier_tags("voice")) == ["friends", "public"]


def test_recall_tags_for_telegram_are_all_tiers():
    assert sorted(agent_mod._recall_tier_tags("telegram")) == ["family", "friends", "private", "public"]


def test_overlay_pushed_only_at_private_clearance():
    assert agent_mod._overlay_allowed("telegram") is True
    assert agent_mod._overlay_allowed("voice") is False


def test_shared_bank_is_role_independent():
    assert agent_mod._memory_bank() == "casa"
