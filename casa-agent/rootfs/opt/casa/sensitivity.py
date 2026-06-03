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

import re

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


# ---------------------------------------------------------------------------
# LLM output parsing (design §2.4)
# ---------------------------------------------------------------------------

_TIER_RE = re.compile(r"\b(private|family|friends|public)\b", re.IGNORECASE)


def parse_tier(text: str | None) -> str | None:
    """Extract a tier token from an LLM/agent emission. Returns the lowercased tier,
    or None if no known tier is present. The caller applies DEFAULT_TIER on None."""
    if not isinstance(text, str):
        return None
    m = _TIER_RE.search(text)
    return m.group(1).lower() if m else None


# First-draft classification prompt. CONVERGED with the maintainer in a later task — treat
# this wording as provisional; the eval set is the source of truth for correctness.
SENSITIVITY_PROMPT = """\
You classify a single fact about the user (or their household) into ONE access tier,
deciding WHO should be allowed to recall it later. Judge sensitivity, not topic.

Tiers (most to least sensitive):
- private  — only the user. Finances, health, account details, intimate or sensitive
             personal matters, anything that would be harmful or embarrassing if others
             learned it.
- family   — the user's household/family may know, but not friends. Family logistics,
             relationships, plans, mildly sensitive household matters.
- friends  — people the user invites in / talks to the home agent may know. Non-sensitive
             preferences and shared context (e.g. "likes the thermostat at 20°C").
- public   — no confidentiality at all; safe for anyone.

Rules:
- When in doubt, choose the MORE private tier (a leak is worse than forgetting).
- Classify the fact's content, independent of where it was said.

Respond with ONLY the single tier word: private, family, friends, or public.
Fact:
"""
