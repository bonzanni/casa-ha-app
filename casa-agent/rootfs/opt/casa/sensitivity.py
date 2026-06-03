# casa-agent/rootfs/opt/casa/sensitivity.py
"""Sensitivity-tier access vocabulary for long-term memory
(design 2026-06-03-tiered-memory-access-design §2).

Tiers are an access ladder, ascending sensitivity:
    public  <  friends  <  family  <  private
A context's CLEARANCE is the highest tier it may read; it reads its own tier and all
LESS-sensitive tiers. Facts are tagged with a tier; recall filters to tiers <= clearance.
Retrieval relevance is Hindsight's job (semantic) — these tiers are purely access control.
"""
from __future__ import annotations

# Ascending sensitivity. Index = sensitivity rank (higher = more private).
TIERS: tuple[str, ...] = ("public", "friends", "family", "private")

# Leak-safe default when a turn is unlabeled on a classified channel (design §2.4).
DEFAULT_TIER = "private"

# Default channel -> clearance. Voice = friends (people in the home, not the open public);
# a private DM is fully trusted. Future speaker-recognition can lift voice above friends.
CLEARANCE_BY_CHANNEL: dict[str, str] = {
    "voice": "friends",
}
_DEFAULT_CLEARANCE = "private"


def _rank(tier: str) -> int:
    return TIERS.index(tier)


def readable_tiers(clearance: str) -> list[str]:
    """Tiers a context at ``clearance`` may read: its own tier + all less sensitive."""
    ceiling = _rank(clearance)
    return [t for t in TIERS if _rank(t) <= ceiling]


def apply_ceiling(tier: str, ceiling: str) -> str:
    """Cap ``tier`` so it is no more sensitive than ``ceiling`` (the channel ceiling)."""
    return tier if _rank(tier) <= _rank(ceiling) else ceiling


def clearance_for_channel(channel: str) -> str:
    """Clearance for a channel (defaults to the most-trusted tier for private surfaces)."""
    return CLEARANCE_BY_CHANNEL.get(channel, _DEFAULT_CLEARANCE)
