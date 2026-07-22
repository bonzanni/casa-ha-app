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
from typing import Any, Sequence

from channel_policy import writes_to_bank
from hindsight_ids import bank_id
from memory_provenance import build_retain_items
from personality_types import RetainedTurn, SpeakerProvenance
from recall_health import RecallPath, default_telemetry, observed_recall
from recall_renderer import Surface, render_recall
from semantic_memory import RecallUnavailable
from sensitivity import clearance_for_channel, readable_tiers
from tier_classifier import classify_tier

logger = logging.getLogger(__name__)


async def delegated_recall(
    semantic_memory: Any, *, query: str, origin_channel: str, max_tokens: int,
    budget: str = "mid", surface: Surface = "text",
    path: RecallPath = "delegated", current_speaker: SpeakerProvenance | None = None,
) -> str:
    """Recall the shared bank at the ORIGINATING context's read-clearance,
    returning an ATTRIBUTED digest (personality Task 11).

    Three-outcome contract (v0.99.0): returns the digest ('' = a GENUINE
    zero-hit search) or raises :class:`RecallUnavailable` when memory could
    not be checked — the two must never be conflated, or the delegated agent
    denies knowledge Casa actually has. Call sites decide how to degrade
    (typically: proceed with no memory block, without claiming absence).

    Task 11: swaps the flat ``recall()`` string for typed ``recall_items()``
    routed through the NEW ``recall_health.observed_recall`` breaker/telemetry
    (``path`` distinguishes the delegated / query_engager / executor_archive
    callers), then renders each hit with its recorded attribution. The exact
    unavailable-vs-zero-hit discipline is UNCHANGED — only success-path
    rendering differs.

    ``budget`` defaults to ``mid``. The v0.68.1 ``low`` default (D-3) was a
    stop-gap for a hindsight-side rerank-latency bug that crossed the 20s
    client budget under concurrent load; once that was fixed hindsight-side,
    ``mid`` (→300 reranked candidates) is the better default — materially
    higher recall quality — and no longer risks the timeout. Reverted v0.69.4.
    Explicit ``budget=`` (e.g. voice → ``low``) still overrides."""
    if not (query or "").strip():
        return ""
    clearance = clearance_for_channel(origin_channel)
    tags = readable_tiers(clearance)
    if current_speaker is None:
        current_speaker = SpeakerProvenance(speaker_kind="system")
    try:
        hits = await observed_recall(
            path=path, telemetry=default_telemetry(),
            operation=lambda: semantic_memory.recall_items(
                bank_id("casa"), query, tags=tags, max_tokens=max_tokens,
                clearance=clearance, budget=budget,
            ),
        )
    except RecallUnavailable:
        # Backend already logged outcome/reason/latency (recorded by
        # observed_recall). RecallProtocolError (a RecallUnavailable subclass)
        # is caught here too — a malformed/untrustworthy envelope is UNAVAILABLE,
        # never a fake zero-hit.
        logger.warning("delegated recall unavailable (channel=%s)", origin_channel)
        raise
    except Exception as exc:  # noqa: BLE001 — typed for callers, never a raw crash
        # Exception TYPE only — repr/traceback could embed the query text,
        # which must never be logged.
        logger.warning(
            "delegated recall failed (channel=%s): %s",
            origin_channel, type(exc).__name__,
        )
        raise RecallUnavailable("backend_error") from exc
    return render_recall(
        hits, current_speaker=current_speaker, surface=surface,
        clearance=clearance, token_budget=max_tokens,
    )


async def retain_delegated(
    semantic_memory: Any, *, origin_channel: str, turns: Sequence[RetainedTurn],
) -> None:
    """Explicitly retain delegated ``turns`` to the shared bank, each classified at
    its TRUE tier AND attributed to its real :class:`SpeakerProvenance` — IFF the
    originating channel is write-trusted (voice → recall-only → nothing).

    Personality Task 10: dropped ``doc_prefix`` — :func:`build_retain_items`
    content-addresses each turn (user_peer- or persona-identity-keyed), so
    re-retain idempotency no longer needs a caller-scoped prefix, and the same
    fact retained across delegations collapses to one document. ``classify_tier``
    is passed by name so tests that monkeypatch ``delegated_memory.classify_tier``
    still take effect. Best-effort; never raises."""
    if not writes_to_bank(origin_channel):
        return
    items = await build_retain_items(turns, classify=classify_tier)
    if not items:
        return
    try:
        await semantic_memory.retain(bank_id("casa"), items, async_=True)
    except Exception:  # noqa: BLE001 — best-effort background write
        logger.warning(
            "delegated retain failed (origin_channel=%s)", origin_channel, exc_info=True,
        )
