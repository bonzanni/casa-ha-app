"""Tests for memory.py — MemoryProvider ABC and NoOpMemory."""

from __future__ import annotations

import pytest

from memory import MemoryProvider, NoOpMemory

pytestmark = pytest.mark.asyncio


def test_cannot_instantiate_abc():
    """MemoryProvider cannot be instantiated directly."""
    with pytest.raises(TypeError):
        MemoryProvider()  # type: ignore[abstract]


class FakeMemoryProvider(MemoryProvider):
    """Minimal concrete provider used to exercise the 3-method surface."""

    def __init__(self) -> None:
        self.ensure_calls: list[tuple[str, str, str]] = []
        self.get_calls: list[tuple[str, str, int, str | None, str]] = []
        self.add_calls: list[tuple[str, str, str, str, str]] = []

    async def ensure_session(
        self, session_id, agent_role, user_peer="nicola",
    ):
        self.ensure_calls.append((session_id, agent_role, user_peer))

    async def get_context(
        self, session_id, agent_role, tokens,
        search_query=None, user_peer="nicola",
    ):
        self.get_calls.append(
            (session_id, agent_role, tokens, search_query, user_peer)
        )
        return f"ctx({session_id},{agent_role},{user_peer})"

    async def add_turn(
        self, session_id, agent_role, user_text, assistant_text,
        user_peer="nicola",
    ):
        self.add_calls.append(
            (session_id, agent_role, user_text, assistant_text, user_peer)
        )


async def test_fake_roundtrip_threads_user_peer():
    mem = FakeMemoryProvider()
    await mem.ensure_session("telegram-1-assistant", "assistant")
    ctx = await mem.get_context(
        "telegram-1-assistant", "assistant", tokens=4000, search_query="hi",
    )
    await mem.add_turn(
        "telegram-1-assistant", "assistant", "hi", "hello",
    )

    assert mem.ensure_calls == [
        ("telegram-1-assistant", "assistant", "nicola"),
    ]
    assert mem.get_calls == [
        ("telegram-1-assistant", "assistant", 4000, "hi", "nicola"),
    ]
    assert mem.add_calls == [
        ("telegram-1-assistant", "assistant", "hi", "hello", "nicola"),
    ]
    assert ctx == "ctx(telegram-1-assistant,assistant,nicola)"


async def test_fake_voice_user_peer_override():
    mem = FakeMemoryProvider()
    await mem.ensure_session(
        "voice-lr-butler", "butler", user_peer="voice_speaker",
    )
    await mem.add_turn(
        "voice-lr-butler", "butler", "lights on", "ok",
        user_peer="voice_speaker",
    )
    assert mem.ensure_calls[0][2] == "voice_speaker"
    assert mem.add_calls[0][4] == "voice_speaker"


async def test_noop_memory_returns_empty_and_stores_nothing():
    mem = NoOpMemory()
    await mem.ensure_session("any", "assistant")
    ctx = await mem.get_context("any", "assistant", tokens=4000)
    await mem.add_turn("any", "assistant", "u", "a")
    assert ctx == ""
