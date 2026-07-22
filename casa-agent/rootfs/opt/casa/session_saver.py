# casa-agent/rootfs/opt/casa/session_saver.py
"""Long-term save orchestration (design §4.2): freshness windows, transcript →
retain items, and the idempotent save_session entry point. Retains whole ended
conversations to Hindsight at session granularity (short-term covers live turns)."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from channel_policy import writes_to_bank
from claude_agent_sdk import get_session_messages
from hindsight_ids import bank_id
from memory_provenance import build_retain_items
from personality_types import RetainedTurn, SpeakerProvenance
from tier_classifier import classify_tier

if TYPE_CHECKING:
    from agent import SessionEntrySnapshot

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
    messages: list, *, speaker_provenance: SpeakerProvenance,
    user_provenance: SpeakerProvenance,
) -> list[dict[str, Any]]:
    """Turn an SDK transcript into provenance-bearing Hindsight retain items
    (design §4.2; tier model §2.4; personality Task 10). Each user turn is
    attributed to ``user_provenance`` (the trusted per-turn ingress identity) and
    each assistant turn to ``speaker_provenance`` (the resident's real persona
    identity), then funneled through :func:`build_retain_items` so every item
    carries its speaker provenance tag + canonical metadata alongside its tier.

    F1 (2026-07-09 bug review): dedup is now owned by ``build_retain_items`` — a
    line repeated N times collapses within-batch, and the content-addressed
    ``document_id`` (user_peer- or persona-identity-keyed) makes the same turn
    retained from any later session upsert to the SAME document instead of
    duplicating. ``classify_tier`` is passed by name so tests that monkeypatch
    ``session_saver.classify_tier`` still take effect."""
    turns: list[RetainedTurn] = []
    for m in messages:
        text = _message_text(getattr(m, "message", None))
        if not text:
            continue
        if getattr(m, "type", "") == "user":
            turns.append(RetainedTurn(text, user_provenance))
        else:
            turns.append(RetainedTurn(text, speaker_provenance))
    if not turns:
        return []
    return await build_retain_items(
        turns, classify=classify_tier, classify_concurrency=_CLASSIFY_CONCURRENCY,
    )


async def save_session(
    channel_key: str, registry, semantic_memory, *, directory: str, channel: str,
) -> bool:
    """Idempotently retain an ended session to long-term memory (design §4.2; tier
    model §2.4; personality Task 10). Channels that fail write-trust (voice —
    recall-only) persist nothing. Atomically claims the entry; the persisted
    speaker/user identities come from the entry's own provenance snapshot (never
    a caller-passed role/user_peer), so a legacy or corrupt entry with no usable
    provenance releases the claim rather than retaining with invented authorship.
    On success retains the per-item tier+provenance-tagged transcript to the
    shared ``casa`` bank and removes the entry."""
    from agent import snapshot_session_entry

    if not writes_to_bank(channel):
        return False  # recall-only channel (e.g. voice): never persists facts
    if not await registry.try_begin_save(channel_key):
        return False  # missing or already being saved (reaper/next-turn race)
    snapshot = snapshot_session_entry(registry.get(channel_key))
    if snapshot is None or snapshot.speaker_provenance is None or snapshot.user_provenance is None:
        logger.debug(
            "save_session: %s has no usable provenance snapshot — releasing claim",
            channel_key,
        )
        await registry.clear_save_claim(channel_key)
        return False
    sid = snapshot.sdk_session_id
    try:
        messages = await asyncio.to_thread(get_session_messages, sid, directory)
        items = await transcript_to_items(
            messages, speaker_provenance=snapshot.speaker_provenance,
            user_provenance=snapshot.user_provenance,
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
    old: "SessionEntrySnapshot", *, directory: str, channel: str, semantic_memory,
) -> None:
    """Retain a specific cold SDK session's transcript to the shared ``casa`` bank,
    OFF the turn's critical path and DECOUPLED from the session registry (no
    try_begin_save/finish_save). Used by the next-turn-after-gap path, where the
    registry entry for this channel is about to be overwritten by the new session —
    so a registry-claiming save (save_session) would race register(); this does not
    touch the registry at all. Channels failing write-trust (voice) retain nothing.

    Personality Task 10: consumes the immutable ``SessionEntrySnapshot`` directly.
    A legacy/corrupt snapshot with no usable speaker/user provenance retains
    NOTHING — memory is never written with invented authorship. The
    content-addressed ``document_id`` keeps re-retain idempotent."""
    if not writes_to_bank(channel):
        return
    if old.speaker_provenance is None or old.user_provenance is None:
        return  # legacy/corrupt snapshot: never retain with invented authorship
    try:
        messages = await asyncio.to_thread(get_session_messages, old.sdk_session_id, directory)
        items = await transcript_to_items(
            messages, speaker_provenance=old.speaker_provenance,
            user_provenance=old.user_provenance,
        )
        if items:
            await semantic_memory.retain(bank_id("casa"), items, async_=True)
    except Exception:  # noqa: BLE001 — background; never surface to the turn
        logger.warning(
            "background cold-session retain failed for sid=%s", old.sdk_session_id,
            exc_info=True,
        )


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
    # Task 10: the reduced save_session reads speaker/user provenance from the
    # entry snapshot itself — no role=/user_peer= to pass here.
    # save_session is idempotent and removes the entry on a successful retain;
    # remove() afterwards guarantees the pointer is cleared even when the save
    # was a no-op (nothing to retain).
    await save_session(
        channel_key, registry, semantic_memory,
        directory=directory, channel=channel,
    )
    await registry.remove(channel_key)
