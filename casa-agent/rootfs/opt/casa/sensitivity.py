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

# Channel -> read clearance. Explicit for every real ingress so the grant is a
# DECISION, not an accident of the fallback (X2, 2026-07-09):
#   telegram = private — the operator's authenticated DM, fully trusted.
#   voice    = friends  — people in the home, not the open public; a future
#                         speaker-ID upgrade can lift a recognised speaker higher.
#   webhook  = private — /invoke + /webhook are gated by the HMAC secret;
#                        per operator decision (2026-07-09) the secret IS the
#                        trust boundary, so a holder reads at full clearance
#                        like the DM.
# NOTE: _DEFAULT_CLEARANCE is still "private" (fail-OPEN) for any UNMAPPED
# channel. Flipping it to fail-closed is a stored decision (touches the
# channel="" boot-replay edge) — see overnight-2026-07-09-worklog.
CLEARANCE_BY_CHANNEL: dict[str, str] = {
    "telegram": "private",
    "voice": "friends",
    "webhook": "private",
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


# Classification prompt — converged with the maintainer via the eval session (2026-06-03).
# The eval set (tests/fixtures/sensitivity_eval.jsonl) is the regression source of truth;
# keep this prompt and that set in sync. Calibration note: `friends` is the BROAD default —
# do not over-escalate.
SENSITIVITY_PROMPT = """\
You assign a single fact about the user (or their household) to ONE access tier, deciding
WHO the user would let recall it later. Judge by "who would the user naturally share this
with?", NOT by topic.

Tiers, most to least sensitive:
- private  — only the user. Money/income/expenses (even household — treat finances as
             private); medical diagnoses, treatments, medications, mental-health matters;
             personal-account credentials (email, bank); intimate or relationship matters;
             undisclosed or in-progress personal decisions; identity-document-level details.
- family   — NARROW. Secrets/credentials for a SHARED SPACE the household relies on but
             friends should not have: the home alarm/disarm code, the MAIN wifi password.
             These are family — NOT private (they are not personal-account logins) and NOT
             friends (the main network/alarm is not guest-facing). Also genuinely
             family-internal sensitive matters (e.g. a relative's private difficulty).
             NOT general household logistics, and NOT money.
- friends  — the DEFAULT for ordinary, mildly-personal, socially-shareable facts: preferences
             (thermostat at 20°C), travel/whereabouts and holiday plans, kids' names/ages,
             birthdays, allergies and other safety info, everyday household logistics (school
             pickup times, visitors, weekly dinners), the guest wifi, pets. Anyone the user
             talks to in the home is friends-or-closer.
- public   — impersonal, general-knowledge, harmless to anyone: bin-collection day, local
             shop hours, the make/model/brand of a household device (thermostat, tap,
             appliance — a brand name is not personal), the user's professional role/employer,
             the colour the living room is painted.

Rules:
1. Judge "who would the user share this with?" — not the topic. A topic is not a tier
   (e.g. medical: an allergy is safety info → friends/public; a diagnosis/medication/
   mental-health matter → private).
2. Money and finances are private, even when household-scoped — amounts, accounts, AND
   invoicing/billing patterns or habits (how the user bills, what lines they put on invoices).
3. Tier a secret by what it protects: a PERSONAL-ACCOUNT credential (email/bank login) →
   private; a SHARED-SPACE household secret (alarm code, MAIN wifi password) → family; a
   GUEST-facing secret (guest wifi) → friends.
4. PII that could verify identity or enable social engineering (e.g. birthdate) is at
   least friends — never public.
5. Undisclosed / in-progress personal matters lean more private.
6. Do NOT over-escalate: friends is the right home for most personal-but-shareable facts.
   Reserve family for shared-space secrets / family-internal sensitive matters, and private
   for genuinely sensitive things.
7. Only when you are genuinely unsure after applying the above, choose the more private
   tier (a leak is worse than forgetting).

Respond with ONLY the single tier word: private, family, friends, or public.
Fact:
"""
