# casa-agent/rootfs/opt/casa/session_saver.py
"""Long-term save orchestration (design §4.2): freshness windows, transcript →
retain items, and the idempotent save_session entry point. Retains whole ended
conversations to Hindsight at session granularity (short-term covers live turns)."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import Any

from channel_policy import writes_to_bank
from channel_trust import user_peer_for_channel
from claude_agent_sdk import get_session_messages
from hindsight_ids import bank_id, content_document_id
from tier_classifier import classify_tier

logger = logging.getLogger(__name__)

# Per-channel freshness windows (spec §3.3). Independent of SESSION_TTL.
_DEFAULT_VOICE_MIN = 30
_DEFAULT_TELEGRAM_H = 12

# M29: bound the concurrency of per-item tier classification (each is a full
# claude-CLI subprocess + LLM turn). Classifying a long transcript serially
# blocked /new for minutes; a bounded gather cuts wall time while capping the
# number of concurrent CLI subprocesses.
_CLASSIFY_CONCURRENCY = 4


def freshness_window(channel: str) -> timedelta:
    """How long a session stays 'live' (resumable) before it goes cold and is
    eligible for save. Env-overridable: FRESHNESS_VOICE_MINUTES,
    FRESHNESS_TELEGRAM_HOURS."""
    if channel == "voice":
        return timedelta(minutes=int(os.environ.get("FRESHNESS_VOICE_MINUTES", _DEFAULT_VOICE_MIN)))
    # telegram + default for any conversational channel
    return timedelta(hours=int(os.environ.get("FRESHNESS_TELEGRAM_HOURS", _DEFAULT_TELEGRAM_H)))


def _message_text(message: Any) -> str:
    """Extract plain text from a SessionMessage.message payload. The payload is
    Any: either a string, or an Anthropic-style {role, content} where content is
    a string or a list of blocks ({type:'text', text:...}). Tool-use/result
    blocks contribute no text."""
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            return "".join(parts).strip()
    return ""


async def transcript_to_items(
    messages: list, *, sdk_session_id: str, user_peer: str,
) -> list[dict[str, Any]]:
    """Turn an SDK transcript into Hindsight retain items (design §4.2; tier model
    §2.4). One item per DISTINCT text-bearing turn, each classified at its TRUE
    sensitivity tier (default-private on uncertainty) and tagged ``[tier]`` —
    Hindsight tags are document-granular, so per-item is the finest partition
    available.

    F1 (2026-07-09 bug review): a repetitive conversation used to emit one item
    per occurrence of a repeated line AND re-retain the same content under a new
    ``document_id`` every time the SDK session rotated — one session produced ~40
    near-duplicate memories, ~50 across four sids. Two dedup layers fix that:

    * **Within-batch:** collapse identical ``(speaker, text)`` turns so a line
      repeated N times in one transcript yields ONE item (first occurrence wins,
      order preserved). ``sdk_session_id`` is now unused for the id but kept in
      the signature for caller stability and future provenance needs.
    * **Cross-session:** ``document_id`` is content-derived
      (:func:`content_document_id`), so the same ``(speaker, text)`` retained
      from any later session upserts to the SAME Hindsight document instead of
      duplicating."""
    # Phase 1 — collect DISTINCT text-bearing turns (first occurrence wins,
    # original order preserved). Dedup key is (speaker, text): the same words
    # from user vs assistant stay distinct.
    seen: set[tuple[str, str]] = set()
    pending: list[tuple[str, str]] = []  # (text, speaker)
    for m in messages:
        text = _message_text(getattr(m, "message", None))
        if not text:
            continue
        speaker = user_peer if getattr(m, "type", "") == "user" else "assistant"
        key = (speaker, text)
        if key in seen:
            continue
        seen.add(key)
        pending.append((text, speaker))
    if not pending:
        return []
    # Phase 2 — classify concurrently, bounded by a semaphore. classify_tier is
    # looked up at call time (module-global) so tests that monkeypatch
    # session_saver.classify_tier still take effect; it never raises (catches
    # all and returns DEFAULT_TIER), so plain gather is safe.
    sem = asyncio.Semaphore(_CLASSIFY_CONCURRENCY)

    async def _classify(text: str) -> str:
        async with sem:
            return await classify_tier(text)

    tiers = await asyncio.gather(*(_classify(t) for t, _ in pending))
    return [
        {
            "content": text,
            "tags": [tier],
            "metadata": {"speaker": speaker},
            "document_id": content_document_id(speaker, text),
        }
        for (text, speaker), tier in zip(pending, tiers)
    ]


async def save_session(
    channel_key: str, registry, semantic_memory, *, role: str, directory: str,
    user_peer: str, channel: str,
) -> bool:
    """Idempotently retain an ended session to long-term memory (design §4.2; tier
    model §2.4). Channels that fail write-trust (voice — recall-only) persist
    nothing. Atomically claims the entry; on success retains the per-item
    tier-tagged transcript to the shared ``casa`` bank and removes the entry."""
    if not writes_to_bank(channel):
        return False  # recall-only channel (e.g. voice): never persists facts
    if not await registry.try_begin_save(channel_key):
        return False  # missing or already being saved (reaper/next-turn race)
    entry = registry.get(channel_key)
    if entry is None:
        logger.error("save_session: entry for %s vanished after claim — releasing", channel_key)
        await registry.clear_save_claim(channel_key)
        return False
    sid = entry.get("sdk_session_id")
    if not sid:
        logger.debug("save_session: %s has no sid — releasing claim", channel_key)
        await registry.clear_save_claim(channel_key)
        return False
    try:
        messages = await asyncio.to_thread(get_session_messages, sid, directory)
        items = await transcript_to_items(
            messages, sdk_session_id=sid, user_peer=user_peer,
        )
        if items:
            await semantic_memory.retain(bank_id("casa"), items, async_=True)
        # Pass the saved sid so a user turn that re-registered this channel
        # mid-save (slow multi-minute reaper retain) is not clobbered (M24).
        await registry.finish_save(channel_key, sid)
        return True
    except Exception as exc:  # noqa: BLE001 — never crash a save; reaper retries
        logger.warning("save_session failed for %s: %s — will retry", channel_key, exc)
        await registry.clear_save_claim(channel_key, sid)
        return False


async def retain_cold_session(
    *, sid: str, role: str, directory: str, user_peer: str, channel: str,
    semantic_memory,
) -> None:
    """Retain a specific cold SDK session's transcript to the shared ``casa`` bank,
    OFF the turn's critical path and DECOUPLED from the session registry (no
    try_begin_save/finish_save). Used by the next-turn-after-gap path, where the
    registry entry for this channel is about to be overwritten by the new session —
    so a registry-claiming save (save_session) would race register(); this does not
    touch the registry at all. Channels failing write-trust (voice) retain nothing.
    document_id=sid:idx keeps the retain idempotent."""
    # role is accepted for caller-symmetry with save_session (which also takes role)
    # and reserved for future per-role filtering; it is not used in the body today.
    if not writes_to_bank(channel):
        return
    if not sid:  # defensive: the gap call site already guards, but this is a public API
        return
    try:
        messages = await asyncio.to_thread(get_session_messages, sid, directory)
        items = await transcript_to_items(
            messages, sdk_session_id=sid, user_peer=user_peer,
        )
        if items:
            await semantic_memory.retain(bank_id("casa"), items, async_=True)
    except Exception:  # noqa: BLE001 — background; never surface to the turn
        logger.warning("background cold-session retain failed for sid=%s", sid, exc_info=True)


async def reset_channel(
    channel_key: str, registry, semantic_memory, *, channel: str,
) -> None:
    """Explicit reset (design §4.2 #2, correction C2): retain the current session,
    then drop the pointer so the next turn starts fresh. Role + transcript
    directory are derived from the registry entry (the caller — e.g. the Telegram
    channel — does not need to know them). If there is no entry there is nothing
    to save; just return (the caller still acks)."""
    # Imported lazily: agent imports session_saver at module load, so a
    # top-level import here would cycle.
    from agent import agent_home_for_role_id, snapshot_session_entry

    # AR-4 (pooling spec): close any warm SDK client for this key FIRST —
    # disconnect sends stdin EOF, which is what makes the CLI flush the
    # transcript .jsonl this save is about to read (SDK #625).
    await registry.notify_reset(channel_key)
    entry = registry.get(channel_key)
    snapshot = snapshot_session_entry(entry)
    if snapshot is None:
        return
    role = snapshot.agent
    # Task 9: entries now store the canonical role_id; derive the transcript
    # cwd from it. A legacy short-role entry (pre-Task-9) falls back to the
    # bare-slug formula so its transcript is still found.
    try:
        directory = agent_home_for_role_id(role)
    except ValueError:
        directory = f"/config/agent-home/{role}"
    user_peer = user_peer_for_channel(channel)
    # save_session is idempotent and removes the entry on a successful retain;
    # remove() afterwards guarantees the pointer is cleared even when the save
    # was a no-op (nothing to retain).
    await save_session(
        channel_key, registry, semantic_memory,
        role=role, directory=directory, user_peer=user_peer, channel=channel,
    )
    await registry.remove(channel_key)
