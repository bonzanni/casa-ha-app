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
    await p.ensure_session("telegram:1:assistant", "assistant")

    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.sessions["telegram:1:assistant"]
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
        "voice:lr:butler", "butler", user_peer="voice_speaker",
    )
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    peers = client.sessions["voice:lr:butler"].add_peers_calls[0]
    names = {peer.name for peer, _ in peers}
    assert names == {"voice_speaker", "butler"}
    assert "nicola" not in names


async def test_get_context_forwards_perspective_and_target(stub_env):
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    # Prime a context the stub will return
    session = client.session("telegram:1:assistant")

    @dataclass
    class _Ctx:
        messages: list = field(default_factory=list)
        summary: Any = None
        peer_representation: Any = None
        peer_card: list = field(default_factory=list)

    session._next_context = _Ctx(peer_card=["x"])

    out = await p.get_context(
        "telegram:1:assistant", "assistant",
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
    session = client.session("telegram:1:assistant")

    @dataclass
    class _Ctx:
        messages: list = field(default_factory=list)
        summary: Any = None
        peer_representation: Any = None
        peer_card: list = field(default_factory=list)

    session._next_context = _Ctx()
    out = await p.get_context(
        "telegram:1:assistant", "assistant", tokens=100,
    )
    assert out == ""


async def test_add_turn_writes_user_and_assistant_messages(stub_env):
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    await p.add_turn(
        "telegram:1:assistant", "assistant",
        user_text="hi", assistant_text="hello",
    )
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.sessions["telegram:1:assistant"]
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
        "voice:lr:butler", "butler",
        user_text="lights on", assistant_text="ok",
        user_peer="voice_speaker",
    )
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.sessions["voice:lr:butler"]
    msgs = session.add_messages_calls[0]
    assert msgs[0].peer_name == "voice_speaker"
    assert msgs[1].peer_name == "butler"
