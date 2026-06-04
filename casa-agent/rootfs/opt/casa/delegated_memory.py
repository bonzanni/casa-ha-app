# casa-agent/rootfs/opt/casa/delegated_memory.py
"""Delegated-context memory bridge (tiered-memory design §3, plan 3).

Specialists / executors / engagements are NOT residents: they are ephemeral
(no session registry → the freshness reaper never sees them). They become
ordinary participants on the ONE shared bank ``casa``, distinguished only by the
clearance/write-trust INHERITED from their originating context (the resident
turn / engagement that spawned them — its channel is on ``origin_var`` /
``engagement.origin``).

- Read  → ``delegated_recall`` : a single ``recall`` at the originating channel's
  read-clearance (``readable_tiers(clearance_for_channel(origin_channel))``).
- Write → ``retain_delegated`` : an EXPLICIT, write-trust-gated, per-item
  tier-classified ``retain`` (the reaper can't catch ephemeral turns). Voice
  (recall-only) writes nothing. Both are best-effort — they never crash a
  delegated turn.
"""
from __future__ import annotations

import logging
from typing import Any

from channel_policy import writes_to_bank
from hindsight_ids import bank_id
from sensitivity import clearance_for_channel, readable_tiers
from tier_classifier import classify_tier

logger = logging.getLogger(__name__)


async def delegated_recall(
    semantic_memory: Any, *, query: str, origin_channel: str, max_tokens: int,
    budget: str = "mid",
) -> str:
    """Recall the shared bank at the ORIGINATING context's read-clearance.
    Best-effort: any error → '' (the delegated turn proceeds with no memory)."""
    if not (query or "").strip():
        return ""
    tags = readable_tiers(clearance_for_channel(origin_channel))
    try:
        return await semantic_memory.recall(
            bank_id("casa"), query, tags=tags, max_tokens=max_tokens, budget=budget,
        )
    except Exception:  # noqa: BLE001 — delegated read must never crash the turn
        logger.warning("delegated recall failed (channel=%s)", origin_channel, exc_info=True)
        return ""


async def retain_delegated(
    semantic_memory: Any, *, origin_channel: str, doc_prefix: str,
    turns: list[tuple[str, str]],
) -> None:
    """Explicitly retain delegated ``turns`` (``(speaker, text)``) to the shared
    bank, each classified at its TRUE tier — IFF the originating channel is
    write-trusted (voice → recall-only → nothing). ``document_id`` =
    ``f"{doc_prefix}:{idx}"`` (idx from the original list) keeps re-retain
    idempotent. Best-effort; never raises."""
    if not writes_to_bank(origin_channel):
        return
    items: list[dict[str, Any]] = []
    for idx, (speaker, text) in enumerate(turns):
        body = (text or "").strip()
        if not body:
            continue
        tier = await classify_tier(body)
        items.append({
            "content": body,
            "tags": [tier],
            "metadata": {"speaker": speaker},
            "document_id": f"{doc_prefix}:{idx}",
        })
    if not items:
        return
    try:
        await semantic_memory.retain(bank_id("casa"), items, async_=True)
    except Exception:  # noqa: BLE001 — best-effort background write
        logger.warning("delegated retain failed (prefix=%s)", doc_prefix, exc_info=True)
