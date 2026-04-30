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
    """Minimal concrete provider used to exercise the 5-method surface."""

    def __init__(self) -> None:
        self.ensure_calls: list[tuple[str, str, str]] = []
        self.get_calls: list[tuple[str, int, str | None, str | None]] = []
        self.add_calls: list[tuple[str, str, str, str, str]] = []
        self.cross_calls: list[tuple[str, str, int, str]] = []
        self.overlay_calls: list[tuple[str, str, str, int]] = []

    async def ensure_session(
        self, session_id, agent_role, user_peer="nicola",
    ):
        self.ensure_calls.append((session_id, agent_role, user_peer))

    async def get_context(
        self, session_id, tokens, search_query=None, agent_role=None,
    ):
        self.get_calls.append((session_id, tokens, search_query, agent_role))
        return f"ctx({session_id})"

    async def peer_overlay_context(
        self, observer_role, user_peer, search_query, tokens,
    ):
        self.overlay_calls.append(
            (observer_role, user_peer, search_query, tokens)
        )
        return ""

    async def add_turn(
        self, session_id, agent_role, user_text, assistant_text,
        user_peer="nicola",
    ):
        self.add_calls.append(
            (session_id, agent_role, user_text, assistant_text, user_peer)
        )

    async def cross_peer_context(
        self, observer_role, query, tokens, user_peer="nicola",
    ):
        self.cross_calls.append((observer_role, query, tokens, user_peer))
        return ""


async def test_fake_roundtrip_threads_user_peer():
    mem = FakeMemoryProvider()
    await mem.ensure_session("telegram-1-assistant", "assistant")
    ctx = await mem.get_context(
        "telegram-1-assistant", tokens=4000, search_query="hi",
    )
    await mem.add_turn(
        "telegram-1-assistant", "assistant", "hi", "hello",
    )

    assert mem.ensure_calls == [
        ("telegram-1-assistant", "assistant", "nicola"),
    ]
    # M3-self (v0.30.0): get_context now also accepts agent_role
    # (forwarded as Honcho's peer_target). Caller didn't supply one
    # here so the recorded value is None.
    assert mem.get_calls == [
        ("telegram-1-assistant", 4000, "hi", None),
    ]
    assert mem.add_calls == [
        ("telegram-1-assistant", "assistant", "hi", "hello", "nicola"),
    ]
    assert ctx == "ctx(telegram-1-assistant)"


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
    ctx = await mem.get_context("any", tokens=4000)
    await mem.add_turn("any", "assistant", "u", "a")
    assert ctx == ""


async def test_noop_cross_peer_context_returns_empty():
    """Spec § 5.2: NoOpMemory returns "" — no Honcho key configured."""
    from memory import NoOpMemory

    p = NoOpMemory()
    out = await p.cross_peer_context(
        observer_role="finance",
        query="what does Finance know about my budget priorities",
        tokens=2000,
    )
    assert out == ""


async def test_noop_cross_peer_context_accepts_user_peer_kwarg():
    """Verify the default `user_peer="nicola"` is honored on NoOp without
    raising — keeps the API surface symmetric with Honcho's impl."""
    from memory import NoOpMemory

    p = NoOpMemory()
    out = await p.cross_peer_context(
        observer_role="finance", query="x", tokens=2000, user_peer="nicola",
    )
    assert out == ""
