"""Unit tests for HonchoMemoryProvider against a stubbed SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


# --- Stubs mirroring the v3 SDK surface used by the provider ---------------


@dataclass
class StubSessionPeerConfig:
    observe_me: bool = False
    observe_others: bool = False


@dataclass
class StubMessage:
    peer_name: str
    content: str


class StubPeer:
    def __init__(self, name: str) -> None:
        self.name = name

    def message(self, content: str) -> StubMessage:
        return StubMessage(peer_name=self.name, content=content)


class StubSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.add_peers_calls: list[list[tuple[StubPeer, StubSessionPeerConfig]]] = []
        self.add_messages_calls: list[list[StubMessage]] = []
        self.context_calls: list[dict[str, Any]] = []
        self._next_context: Any = None

    def add_peers(self, peers):
        self.add_peers_calls.append(list(peers))

    def add_messages(self, messages):
        self.add_messages_calls.append(list(messages))

    def context(self, tokens, peer_target, peer_perspective, search_query=None):
        self.context_calls.append({
            "tokens": tokens,
            "peer_target": peer_target,
            "peer_perspective": peer_perspective,
            "search_query": search_query,
        })
        return self._next_context


class StubHoncho:
    def __init__(self, api_key: str, base_url: str, workspace_id: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.workspace_id = workspace_id
        self.sessions: dict[str, StubSession] = {}
        self.peers: dict[str, StubPeer] = {}

    def session(self, session_id: str) -> StubSession:
        self.sessions.setdefault(session_id, StubSession(session_id))
        return self.sessions[session_id]

    def peer(self, name: str) -> StubPeer:
        self.peers.setdefault(name, StubPeer(name))
        return self.peers[name]


@pytest.fixture
def stub_env(monkeypatch):
    """Patch memory module's Honcho + SessionPeerConfig symbols."""
    import memory

    monkeypatch.setattr(memory, "Honcho", StubHoncho, raising=True)
    monkeypatch.setattr(
        memory, "SessionPeerConfig", StubSessionPeerConfig, raising=True,
    )
    return memory


# --- Tests ------------------------------------------------------------------


async def test_ensure_session_adds_peers_with_correct_flags(stub_env):
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    await p.ensure_session("telegram-1-assistant", "assistant")

    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.sessions["telegram-1-assistant"]
    assert len(session.add_peers_calls) == 1
    peers = session.add_peers_calls[0]
    names_flags = {peer.name: cfg for peer, cfg in peers}
    assert names_flags["nicola"].observe_me is True
    assert names_flags["nicola"].observe_others is False
    assert names_flags["assistant"].observe_me is False
    assert names_flags["assistant"].observe_others is True


async def test_ensure_session_voice_attributes_to_voice_speaker(stub_env):
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    await p.ensure_session(
        "voice-lr-butler", "butler", user_peer="voice_speaker",
    )
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    peers = client.sessions["voice-lr-butler"].add_peers_calls[0]
    names = {peer.name for peer, _ in peers}
    assert names == {"voice_speaker", "butler"}
    assert "nicola" not in names


async def test_get_context_forwards_perspective_and_target(stub_env):
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    # Prime a context the stub will return
    session = client.session("telegram-1-assistant")

    @dataclass
    class _Ctx:
        messages: list = field(default_factory=list)
        summary: Any = None
        peer_representation: Any = None
        peer_card: list = field(default_factory=list)

    session._next_context = _Ctx(peer_card=["x"])

    out = await p.get_context(
        "telegram-1-assistant", "assistant",
        tokens=4000, search_query="hi",
    )
    call = session.context_calls[0]
    assert call["peer_target"] == "nicola"
    assert call["peer_perspective"] == "assistant"
    assert call["tokens"] == 4000
    assert call["search_query"] == "hi"
    assert "## What I know about you" in out


async def test_get_context_empty_returns_empty_string(stub_env):
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.session("telegram-1-assistant")

    @dataclass
    class _Ctx:
        messages: list = field(default_factory=list)
        summary: Any = None
        peer_representation: Any = None
        peer_card: list = field(default_factory=list)

    session._next_context = _Ctx()
    out = await p.get_context(
        "telegram-1-assistant", "assistant", tokens=100,
    )
    assert out == ""


async def test_add_turn_writes_user_and_assistant_messages(stub_env):
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    await p.add_turn(
        "telegram-1-assistant", "assistant",
        user_text="hi", assistant_text="hello",
    )
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.sessions["telegram-1-assistant"]
    assert len(session.add_messages_calls) == 1
    msgs = session.add_messages_calls[0]
    assert msgs[0].peer_name == "nicola"
    assert msgs[0].content == "hi"
    assert msgs[1].peer_name == "assistant"
    assert msgs[1].content == "hello"


async def test_add_turn_voice_uses_voice_speaker(stub_env):
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    await p.add_turn(
        "voice-lr-butler", "butler",
        user_text="lights on", assistant_text="ok",
        user_peer="voice_speaker",
    )
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.sessions["voice-lr-butler"]
    msgs = session.add_messages_calls[0]
    assert msgs[0].peer_name == "voice_speaker"
    assert msgs[1].peer_name == "butler"


async def test_get_context_renders_summary_and_peer_repr_when_honcho_returns_them(stub_env):
    """M3a — closes the spec § 9 'real-Honcho-response coverage' gap.

    Existing tests prime the SDK stub with summary=None /
    peer_representation=None. This one populates both — plus a peer_card
    bullet and recent messages — and asserts the rendered output
    contains all four canonical sections from `_render`. Verifies the
    end-to-end wiring SDK return → HonchoMemoryProvider.get_context →
    _render → markdown digest, which is the integration contract spec
    § 9 names but no test exercises today.

    The duck-type matches honcho-ai==2.1.1's SessionContext (verified by
    Task A.1) — `summary` is an object with `.content: str`,
    `peer_representation` is `str | None`, `messages` is
    `list[StubMessage]`, `peer_card` is `list[str]`.
    """
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.session("telegram-1-domestic-assistant")

    @dataclass
    class _Summary:
        content: str

    @dataclass
    class _Ctx:
        messages: list = field(default_factory=list)
        summary: Any = None
        peer_representation: Any = None
        peer_card: list = field(default_factory=list)

    session._next_context = _Ctx(
        messages=[
            StubMessage(peer_name="nicola", content="what's the weather"),
            StubMessage(peer_name="assistant", content="sunny, 18C"),
        ],
        summary=_Summary(
            content="Earlier we discussed the user's morning routine.",
        ),
        peer_representation=(
            "User values brevity and prefers Celsius."
        ),
        peer_card=["lives in Amsterdam", "drinks oat milk"],
    )

    out = await p.get_context(
        "telegram-1-domestic-assistant", "assistant",
        tokens=4000, search_query="what's the weather",
    )

    # All four canonical sections present, in the order _render emits.
    # _render's order is (memory.py:111-131): peer_card → summary →
    # peer_representation → messages. Assert each substring AND assert
    # ordering by index comparisons so a future _render reordering
    # surfaces here, not silently in production.
    idx_card = out.find("## What I know about you")
    idx_summary = out.find("## Summary so far")
    idx_perspective = out.find("## My perspective")
    idx_recent = out.find("## Recent exchanges")
    assert idx_card != -1, f"missing peer_card section. out={out!r}"
    assert idx_summary != -1, f"missing summary section. out={out!r}"
    assert idx_perspective != -1, f"missing peer_repr section. out={out!r}"
    assert idx_recent != -1, f"missing messages section. out={out!r}"
    assert idx_card < idx_summary < idx_perspective < idx_recent

    # Content fidelity: verify each section carries the populated value
    # (regression guard against a future _render that emits headers but
    # drops bodies).
    assert "- lives in Amsterdam" in out
    assert "- drinks oat milk" in out
    assert "Earlier we discussed the user's morning routine." in out
    assert "User values brevity and prefers Celsius." in out
    assert "[nicola] what's the weather" in out
    assert "[assistant] sunny, 18C" in out

    # The SDK call still receives the right peer_target / peer_perspective
    # / search_query forwarding (M2 baseline; reverify here so the new
    # test is self-sufficient as a single-test sanity check).
    call = session.context_calls[0]
    assert call["peer_target"] == "nicola"
    assert call["peer_perspective"] == "assistant"
    assert call["search_query"] == "what's the weather"
    assert call["tokens"] == 4000


async def test_get_context_emits_memory_call_log(stub_env, caplog):
    """M3b — Honcho.get_context must emit one `memory_call` log line per
    call with backend, session_id, agent_role, t_ms, peer_count,
    summary_present, peer_repr_present, cache_hit. Provider-level
    emission so the SDK return is in scope for the present/count fields.

    Cache_hit is False on this path — wrapper-bypass emission tested in
    test_memory_cached.py."""
    import logging

    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.session("telegram-1-domestic-assistant")

    @dataclass
    class _Summary:
        content: str

    @dataclass
    class _Ctx:
        messages: list = field(default_factory=list)
        summary: Any = None
        peer_representation: Any = None
        peer_card: list = field(default_factory=list)

    session._next_context = _Ctx(
        messages=[
            StubMessage(peer_name="nicola", content="hi"),
            StubMessage(peer_name="assistant", content="hello"),
        ],
        summary=_Summary(content="prior chat"),
        peer_representation="user is friendly",
        peer_card=[],
    )

    with caplog.at_level(logging.INFO, logger="memory"):
        await p.get_context(
            "telegram-1-domestic-assistant", "assistant",
            tokens=2000, search_query="hi",
        )

    # Exactly one memory_call line on this single get_context call.
    records = [r for r in caplog.records if r.message == "memory_call"]
    assert len(records) == 1, (
        f"expected 1 memory_call, got {len(records)}: "
        f"{[r.message for r in caplog.records]}"
    )
    rec = records[0]
    assert rec.backend == "honcho"
    assert rec.session_id == "telegram-1-domestic-assistant"
    assert rec.agent_role == "assistant"
    assert isinstance(rec.t_ms, int) and rec.t_ms >= 0
    assert rec.peer_count == 2
    assert rec.summary_present is True
    assert rec.peer_repr_present is True
    assert rec.cache_hit is False


async def test_get_context_memory_call_when_summary_missing(stub_env, caplog):
    """summary_present + peer_repr_present must be False when the SDK
    returns None for those fields — distinct from absent (no logging
    at all)."""
    import logging

    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.session("telegram-1-domestic-assistant")

    @dataclass
    class _Ctx:
        messages: list = field(default_factory=list)
        summary: Any = None
        peer_representation: Any = None
        peer_card: list = field(default_factory=list)

    session._next_context = _Ctx(
        messages=[StubMessage(peer_name="nicola", content="hi")],
    )

    with caplog.at_level(logging.INFO, logger="memory"):
        await p.get_context(
            "telegram-1-domestic-assistant", "assistant", tokens=500,
        )

    rec = [r for r in caplog.records if r.message == "memory_call"][0]
    assert rec.summary_present is False
    assert rec.peer_repr_present is False
    assert rec.peer_count == 1
