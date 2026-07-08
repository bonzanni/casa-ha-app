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
from hindsight_ids import bank_id
from tier_classifier import classify_tier

logger = logging.getLogger(__name__)

# Per-channel freshness windows (spec §3.3). Independent of SESSION_TTL.
_DEFAULT_VOICE_MIN = 30
_DEFAULT_TELEGRAM_H = 12


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
    §2.4). One item per text-bearing turn, each classified at its TRUE sensitivity
    tier (default-private on uncertainty) and tagged ``[tier]`` — Hindsight tags are
    document-granular, so per-item is the finest partition available. Deterministic
    ``document_id`` = ``f"{sid}:{idx}"`` keeps a re-retain idempotent."""
    items: list[dict[str, Any]] = []
    for idx, m in enumerate(messages):
        text = _message_text(getattr(m, "message", None))
        if not text:
            continue
        speaker = user_peer if getattr(m, "type", "") == "user" else "assistant"
        tier = await classify_tier(text)
        items.append({
            "content": text,
            "tags": [tier],
            "metadata": {"speaker": speaker},
            "document_id": f"{sdk_session_id}:{idx}",
        })
    return items


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
    entry = registry.get(channel_key)
    if entry is None:
        return
    role = entry.get("agent", "assistant")
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
