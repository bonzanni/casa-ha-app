"""Tests for memory.py -- memory provider abstraction."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from memory import MemoryProvider

# ------------------------------------------------------------------
# ABC enforcement
# ------------------------------------------------------------------


def test_cannot_instantiate_abc():
    """MemoryProvider cannot be instantiated directly."""
    with pytest.raises(TypeError):
        MemoryProvider()  # type: ignore[abstract]


# ------------------------------------------------------------------
# Fake in-memory implementation for testing
# ------------------------------------------------------------------


class FakeMemoryProvider(MemoryProvider):
    """Simple dict-backed memory provider for unit tests."""

    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}  # session_id -> peer_id
        self._messages: list[dict[str, Any]] = []

    async def get_context(
        self,
        peer_id: str,
        token_budget: int,
        exclude_tags: list[str] | None = None,
    ) -> str:
        exclude = set(exclude_tags or [])
        parts: list[str] = []
        budget_used = 0
        for msg in self._messages:
            if msg["peer_id"] != peer_id:
                continue
            msg_tags = set(msg.get("tags") or [])
            if msg_tags & exclude:
                continue
            text = msg["content"]
            # Very rough token estimate: 1 token ~ 4 chars
            cost = len(text) // 4 + 1
            if budget_used + cost > token_budget:
                break
            parts.append(text)
            budget_used += cost
        return "\n".join(parts)

    async def store_message(
        self,
        session_id: str,
        peer_id: str,
        content: str,
        role: str = "user",
        tags: list[str] | None = None,
    ) -> None:
        self._messages.append(
            {
                "session_id": session_id,
                "peer_id": peer_id,
                "content": content,
                "role": role,
                "tags": tags,
            }
        )

    async def create_session(self, peer_id: str) -> str:
        sid = str(uuid.uuid4())
        self._sessions[sid] = peer_id
        return sid

    async def close_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


# ------------------------------------------------------------------
# Roundtrip tests
# ------------------------------------------------------------------


async def test_roundtrip():
    """Create session, store a message, retrieve context."""
    mem = FakeMemoryProvider()
    sid = await mem.create_session("alice")
    await mem.store_message(sid, "alice", "Hello world", role="user")

    ctx = await mem.get_context("alice", token_budget=4000)
    assert "Hello world" in ctx


async def test_tag_filtering():
    """Excluded tags are omitted from context."""
    mem = FakeMemoryProvider()
    sid = await mem.create_session("bob")
    await mem.store_message(sid, "bob", "public info", tags=["general"])
    await mem.store_message(sid, "bob", "secret stuff", tags=["private"])

    ctx_all = await mem.get_context("bob", token_budget=4000)
    assert "public info" in ctx_all
    assert "secret stuff" in ctx_all

    ctx_filtered = await mem.get_context(
        "bob", token_budget=4000, exclude_tags=["private"]
    )
    assert "public info" in ctx_filtered
    assert "secret stuff" not in ctx_filtered
