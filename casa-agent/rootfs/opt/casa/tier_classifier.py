# casa-agent/rootfs/opt/casa/tier_classifier.py
"""Per-item sensitivity-tier classifier (design §2.4, revised 2026-06-04).

Runs the eval-validated SENSITIVITY_PROMPT (sensitivity.py, v0.42.1, live
accuracy 0.94–0.97) as a one-shot SDK query over a single retained item's text,
returning its access tier. Used by the freshness reaper / save path — OFF the
turn's critical path — so the per-turn hot path makes no classification call.

Leak-safe: any uncertainty, empty content, or backend error → DEFAULT_TIER
(``private``), so a mis-handled fact is forgotten at lower clearances rather than
leaked. Voice never reaches here (voice is recall-only — see channel_policy)."""
from __future__ import annotations

import logging

from sensitivity import DEFAULT_TIER, SENSITIVITY_PROMPT, parse_tier

logger = logging.getLogger(__name__)


async def classify_tier(content: str) -> str:
    """Classify one fact/item into a sensitivity tier. Returns a member of TIERS;
    DEFAULT_TIER on blank input, an unparseable reply, or any backend error."""
    text = (content or "").strip()
    if not text:
        return DEFAULT_TIER
    import claude_agent_sdk as sdk

    opts = sdk.ClaudeAgentOptions(
        system_prompt=SENSITIVITY_PROMPT, max_turns=1, allowed_tools=[],
        # NOT bypassPermissions: that makes the SDK pass
        # ``--dangerously-skip-permissions`` to the bundled ``claude`` CLI, which
        # refuses to run as root/sudo — and HA add-ons run as root, so the call
        # fails and every item silently defaults to ``private``. With
        # ``allowed_tools=[]`` there is nothing to approve, so acceptEdits (the
        # mode the rest of casa runs as root) never prompts and works.
        permission_mode="acceptEdits",
    )
    reply = ""
    try:
        async for msg in sdk.query(prompt=text, options=opts):
            if isinstance(msg, sdk.AssistantMessage):
                for block in getattr(msg, "content", []) or []:
                    t = getattr(block, "text", None)
                    if isinstance(t, str):
                        reply += t
    except Exception:  # noqa: BLE001 — classifier must never crash a save
        logger.warning("tier classification failed; defaulting to %s", DEFAULT_TIER, exc_info=True)
        return DEFAULT_TIER
    return parse_tier(reply) or DEFAULT_TIER
