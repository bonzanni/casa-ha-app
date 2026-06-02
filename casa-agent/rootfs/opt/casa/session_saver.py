# casa-agent/rootfs/opt/casa/session_saver.py
"""Long-term save orchestration (spec §4.2): freshness windows, transcript →
retain items, and the idempotent save_session entry point. Retains whole ended
conversations to Hindsight at session granularity (short-term covers live turns)."""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

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
