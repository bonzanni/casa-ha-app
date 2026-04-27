"""Integration coverage for L1: meta scope is readable on text channel,
filtered out on voice channel (M4 v0.16.0)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

# These imports drive a minimal Agent._process invocation. If the agent-
# bootstrap surface is too heavy for unit testing, this becomes a
# smoke-style test that asserts only the partition behavior of the
# scope_registry — see Step 3 fallback.

from scope_registry import ScopeRegistry, load_scope_library


def _scopes_yaml(tmp_path):
    p = tmp_path / "scopes.yaml"
    p.write_text(
        "schema_version: 2\n"
        "scopes:\n"
        "  personal:\n"
        "    minimum_trust: authenticated\n"
        "    kind: topical\n"
        "    description: |\n"
        "      private life, friendships, weekend plans, hobbies, family\n"
        "  house:\n"
        "    minimum_trust: household-shared\n"
        "    kind: topical\n"
        "    description: |\n"
        "      house, home, household, appliances, lights, sensors\n"
        "  meta:\n"
        "    minimum_trust: authenticated\n"
        "    kind: system\n",
        encoding="utf-8",
    )
    return str(p)


def test_meta_appears_in_active_for_authenticated_channel(tmp_path):
    """Read-path partition: authenticated readable → meta is system + active."""
    lib = load_scope_library(_scopes_yaml(tmp_path))
    reg = ScopeRegistry(lib)
    # Skip embedding model; degraded mode flat-scores topical.
    reg._degraded = True

    readable = reg.filter_readable(
        ["personal", "house", "meta"], "authenticated",
    )
    # authenticated (rank 1) can read scopes whose minimum_trust is
    # equal-or-less-privileged: authenticated, household-shared, public.
    assert "meta" in readable
    assert "personal" in readable
    assert "house" in readable  # household-shared is below authenticated

    system_readable = [s for s in readable if reg.kind(s) == "system"]
    topical_readable = [s for s in readable if reg.kind(s) == "topical"]
    assert system_readable == ["meta"]
    assert "personal" in topical_readable
    assert "house" in topical_readable

    scores = reg.score("anything", topical_readable)
    active_topical = reg.active_from_scores(scores, "personal")
    active = system_readable + active_topical
    assert "meta" in active


def test_meta_filtered_out_for_household_shared_voice(tmp_path):
    """Voice (household-shared trust) cannot see authenticated-only meta."""
    lib = load_scope_library(_scopes_yaml(tmp_path))
    reg = ScopeRegistry(lib)
    reg._degraded = True

    readable = reg.filter_readable(
        ["personal", "house", "meta"], "household-shared",
    )
    assert "meta" not in readable
    assert "personal" not in readable  # authenticated > household-shared
    assert "house" in readable


def test_argmax_picks_topical_only_even_with_meta_in_readable(tmp_path):
    """M2.G6 origin stamp must NOT pick meta — meta has no embedding."""
    lib = load_scope_library(_scopes_yaml(tmp_path))
    reg = ScopeRegistry(lib)
    reg._degraded = True

    readable = reg.filter_readable(
        ["personal", "meta"], "authenticated",
    )
    topical_readable = [s for s in readable if reg.kind(s) == "topical"]
    scores = reg.score("anything", topical_readable)
    winner = reg.argmax_scope(scores, "personal")
    assert winner == "personal"
