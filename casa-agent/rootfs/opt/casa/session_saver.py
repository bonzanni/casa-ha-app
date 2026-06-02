# casa-agent/rootfs/opt/casa/session_saver.py
"""Long-term save orchestration (spec §4.2): freshness windows, transcript →
retain items, and the idempotent save_session entry point. Retains whole ended
conversations to Hindsight at session granularity (short-term covers live turns)."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import Any

from claude_agent_sdk import get_session_messages
from hindsight_ids import bank_id

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


def transcript_to_items(
    messages: list, *, sdk_session_id: str, write_scope: str, user_peer: str,
) -> list[dict[str, Any]]:
    """Turn an SDK transcript (get_session_messages output) into Hindsight
    retain items (spec §4.2). One item per text-bearing user/assistant turn;
    deterministic ``document_id`` = ``f"{sid}:{idx}"`` so a re-retain is
    idempotent (Hindsight upsert is destructive-replace — spec §8)."""
    items: list[dict[str, Any]] = []
    for idx, m in enumerate(messages):
        text = _message_text(getattr(m, "message", None))
        if not text:
            continue
        speaker = user_peer if getattr(m, "type", "") == "user" else "assistant"
        items.append({
            "content": text,
            "tags": [write_scope],
            "metadata": {"speaker": speaker},
            "document_id": f"{sdk_session_id}:{idx}",
        })
    return items


async def save_session(
    channel_key: str, registry, semantic_memory, *, role: str, directory: str,
    user_peer: str,
) -> bool:
    """Idempotently retain an ended session to long-term memory (spec §4.2).
    Atomically claims the entry (try_begin_save); on success retains the
    transcript and removes the entry; on failure releases the claim for retry.
    Returns True iff the session was successfully processed (retain performed, or
    skipped for an empty transcript); False if the claim could not be taken or
    the retain failed."""
    if not await registry.try_begin_save(channel_key):
        return False  # missing or already being saved (reaper/next-turn race)
    entry = registry.get(channel_key)
    if entry is None:
        # Invariant violation: we just claimed it under lock, so it should exist.
        logger.error("save_session: entry for %s vanished after claim — releasing", channel_key)
        await registry.clear_save_claim(channel_key)
        return False
    sid = entry.get("sdk_session_id")
    write_scope = entry.get("write_scope")
    if not sid or not write_scope:
        # Legitimate: no SDK session or no classified scope — nothing to retain.
        logger.debug("save_session: %s has no sid/write_scope — releasing claim", channel_key)
        await registry.clear_save_claim(channel_key)
        return False
    try:
        messages = await asyncio.to_thread(get_session_messages, sid, directory)
        items = transcript_to_items(
            messages, sdk_session_id=sid, write_scope=write_scope, user_peer=user_peer,
        )
        if items:
            # async_=True: retain off the critical path (accepted, not awaited to completion)
            await semantic_memory.retain(bank_id("casa", role), items, async_=True)
        await registry.finish_save(channel_key)
        return True
    except Exception as exc:  # noqa: BLE001 — never crash a save; reaper retries
        logger.warning("save_session failed for %s: %s — will retry", channel_key, exc)
        await registry.clear_save_claim(channel_key)
        return False
