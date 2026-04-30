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
    """Mirror of honcho-ai v3 Message — `peer_id` matches the OpenAPI
    components.schemas.Message field name (verified Context7 at
    plan-write 2026-04-29). Pre-Phase-2 this used `peer_name`, mirroring
    the SQLite-side shape; that wrong-shape stub is what hid E-1."""
    peer_id: str
    content: str


class StubPeer:
    def __init__(self, name: str) -> None:
        self.name = name

    def message(self, content: str) -> StubMessage:
        return StubMessage(peer_id=self.name, content=content)


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

    def context(self, tokens, search_query=None):
        # Phase 5 — peer_target / peer_perspective dropped: get_context
        # now issues a Honcho-native session.context() call with no
        # peer-overlay parameters. Overlay fetch moved to
        # peer_overlay_context (peer.context()).
        self.context_calls.append({
            "tokens": tokens,
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


async def test_get_context_does_not_pass_peer_target_or_perspective(stub_env):
    """Phase 5 — get_context now omits peer_target/peer_perspective.
    Peer overlay is fetched separately via peer_overlay_context.
    Replaces the M2-era M3a forwards-perspective-and-target assertion."""
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.session("telegram-1-personal-assistant")

    @dataclass
    class _Ctx:
        messages: list = field(default_factory=list)
        summary: Any = None

    session._next_context = _Ctx()
    await p.get_context(
        "telegram-1-personal-assistant",
        tokens=600, search_query="hi",
    )
    call = session.context_calls[0]
    assert call["tokens"] == 600
    assert call["search_query"] == "hi"
    assert "peer_target" not in call
    assert "peer_perspective" not in call


async def test_get_context_empty_returns_empty_string(stub_env):
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.session("telegram-1-assistant")

    @dataclass
    class _Ctx:
        messages: list = field(default_factory=list)
        summary: Any = None

    session._next_context = _Ctx()
    out = await p.get_context(
        "telegram-1-assistant", tokens=100,
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
    assert msgs[0].peer_id == "nicola"
    assert msgs[0].content == "hi"
    assert msgs[1].peer_id == "assistant"
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
    assert msgs[0].peer_id == "voice_speaker"
    assert msgs[1].peer_id == "butler"


async def test_get_context_renders_summary_and_messages_when_honcho_returns_them(stub_env):
    """Phase 5 — scope-only render contract. Verifies end-to-end wiring
    SDK return → HonchoMemoryProvider.get_context → _render_session →
    markdown digest. peer_card / peer_representation coverage has moved
    to test_peer_overlay_context_renders_self_perspective_headings;
    _render_session no longer emits those sections (A.3)."""
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

    session._next_context = _Ctx(
        messages=[
            StubMessage(peer_id="nicola", content="what's the weather"),
            StubMessage(peer_id="assistant", content="sunny, 18C"),
        ],
        summary=_Summary(
            content="Earlier we discussed the user's morning routine.",
        ),
    )

    out = await p.get_context(
        "telegram-1-domestic-assistant",
        tokens=4000, search_query="what's the weather",
    )

    # Two scope-level sections present in _render_session order:
    # summary → messages. peer_card / peer_representation MUST NOT
    # appear (Phase 5 contract — those belong to peer_overlay_context).
    idx_summary = out.find("## Summary so far")
    idx_recent = out.find("## Recent exchanges")
    assert idx_summary != -1, f"missing summary section. out={out!r}"
    assert idx_recent != -1, f"missing messages section. out={out!r}"
    assert idx_summary < idx_recent
    assert "## What I know about you" not in out
    assert "## My perspective" not in out

    # Content fidelity
    assert "Earlier we discussed the user's morning routine." in out
    assert "[nicola] what's the weather" in out
    assert "[assistant] sunny, 18C" in out

    # SDK call shape: tokens + search_query only (no peer_target/perspective)
    call = session.context_calls[0]
    assert call["tokens"] == 4000
    assert call["search_query"] == "what's the weather"
    assert "peer_target" not in call
    assert "peer_perspective" not in call


async def test_get_context_emits_memory_call_log(stub_env, caplog):
    """Phase 5 — get_context emits one `memory_call` line per call with
    backend, session_id, agent_role="?", t_ms, peer_count,
    summary_present, peer_repr_present=False, cache_hit. Provider-level
    emission so the SDK return is in scope for the present/count fields.

    agent_role: peer overlay moved to peer_overlay_context, so role is
    no longer threaded into get_context — emits literal "?".
    peer_repr_present: False by construction on this scope-only path.

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

    session._next_context = _Ctx(
        messages=[
            StubMessage(peer_id="nicola", content="hi"),
            StubMessage(peer_id="assistant", content="hello"),
        ],
        summary=_Summary(content="prior chat"),
    )

    with caplog.at_level(logging.INFO, logger="memory"):
        await p.get_context(
            "telegram-1-domestic-assistant",
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
    assert rec.agent_role == "?"   # role no longer threaded to this layer
    assert isinstance(rec.t_ms, int) and rec.t_ms >= 0
    assert rec.peer_count == 2
    assert rec.summary_present is True
    assert rec.peer_repr_present is False   # no peer overlay on this path
    assert rec.cache_hit is False
    # M6 § 9 — call_type field distinguishes self vs cross_peer reads
    assert getattr(rec, "call_type", None) == "self"


async def test_get_context_memory_call_when_summary_missing(stub_env, caplog):
    """summary_present must be False when the SDK returns None for that
    field — distinct from absent (no logging at all). peer_repr_present
    is always False on this scope-only path."""
    import logging

    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client: StubHoncho = p._client  # type: ignore[attr-defined]
    session = client.session("telegram-1-domestic-assistant")

    @dataclass
    class _Ctx:
        messages: list = field(default_factory=list)
        summary: Any = None

    session._next_context = _Ctx(
        messages=[StubMessage(peer_id="nicola", content="hi")],
    )

    with caplog.at_level(logging.INFO, logger="memory"):
        await p.get_context(
            "telegram-1-domestic-assistant", tokens=500,
        )

    rec = [r for r in caplog.records if r.message == "memory_call"][0]
    assert rec.summary_present is False
    assert rec.peer_repr_present is False
    assert rec.peer_count == 1
    # M6 § 9 — call_type field distinguishes self vs cross_peer reads
    assert getattr(rec, "call_type", None) == "self"


async def test_cross_peer_context_renders_real_response_shape(stub_env):
    """M6 — closes the spec § 4 'cross-peer real-shape' coverage.

    Mirrors M3a's pattern: prime the SDK stub with a populated
    peer.context() return shape (peer_card + representation), assert
    _render_peer_context produces both sections.

    The duck-type matches honcho-ai==2.1.1's peer.context() return
    (spec § 4 quotes the SDK reference).
    """
    from dataclasses import dataclass, field
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client: StubHoncho = p._client  # type: ignore[attr-defined]

    # Existing StubHoncho.peer() returns a StubPeer that lacks .context().
    # Replace the finance peer with a fixture that does have it.
    @dataclass
    class _RealishPeerContext:
        peer_card: list = field(default_factory=list)
        representation: object = None

    class _PeerWithContext:
        def __init__(self, name):
            self.name = name
            self.context_calls = []
            self._next = None
        def message(self, content):
            return StubMessage(peer_id=self.name, content=content)
        def context(self, target, search_query):
            # Phase 5 / A.0: tokens kwarg dropped — honcho-ai 2.1.1's
            # Peer.context() rejects it via @validate_call.
            self.context_calls.append((target, search_query))
            return self._next

    fp = _PeerWithContext("finance")
    fp._next = _RealishPeerContext(
        peer_card=["prioritizes Q2 invoicing", "ENPICOM primary client"],
        representation=(
            "User asked Finance to handle outstanding invoices via "
            "the Q2 batch."
        ),
    )
    client.peers["finance"] = fp

    out = await p.cross_peer_context(
        observer_role="finance",
        query="what does Finance know about my priorities",
        tokens=2000,
    )

    assert "## What Finance knows about you (cross-role)" in out
    assert "- prioritizes Q2 invoicing" in out
    assert "- ENPICOM primary client" in out
    assert "User asked Finance" in out
    # SDK call shape verified — note no `tokens` (A.0 finding)
    assert fp.context_calls == [
        ("nicola", "what does Finance know about my priorities"),
    ]


async def test_cross_peer_emits_memory_call_with_call_type_cross_peer(
    stub_env, caplog,
):
    """M6 § 9 — cross-peer reads emit memory_call with call_type=
    "cross_peer" and field reinterpretation per spec."""
    import logging
    from dataclasses import dataclass, field
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client: StubHoncho = p._client  # type: ignore[attr-defined]

    @dataclass
    class _PCtx:
        peer_card: list = field(default_factory=lambda: ["a", "b", "c"])
        representation: object = "some accumulated theory of mind"

    class _Peer:
        def __init__(self, name):
            self.name = name
            self._next = _PCtx()
        def message(self, content):
            return StubMessage(peer_id=self.name, content=content)
        def context(self, target, search_query):
            # Phase 5 / A.0: tokens kwarg dropped — see render-side cap.
            return self._next

    client.peers["finance"] = _Peer("finance")

    with caplog.at_level(logging.INFO, logger="memory"):
        await p.cross_peer_context(
            observer_role="finance", query="x", tokens=2000,
        )

    records = [r for r in caplog.records if r.message == "memory_call"]
    assert len(records) == 1, f"expected 1 memory_call, got {len(records)}"
    rec = records[0]
    assert getattr(rec, "call_type", None) == "cross_peer"
    assert rec.backend == "honcho"
    assert rec.agent_role == "finance"
    assert rec.peer_count == 3            # peer_card length
    assert rec.summary_present is False   # no summary on peer.context
    assert rec.peer_repr_present is True  # representation populated
    assert rec.cache_hit is False


# --- Phase 5 peer_overlay_context tests -------------------------------------
#
# Stubs duplicated inline (parity with test_memory_cross_peer.py) so this file
# stays self-contained: cross-file pytest fixture import via plain `from
# test_memory_cross_peer import ...` works for symbols but not for `stub_env`
# fixtures (those need conftest.py promotion to be cleanly reusable). The two
# Stub classes below are <50 LOC; the duplication keeps fixture wiring local.


@dataclass
class StubPeerContext:
    """Mirror of honcho-ai 2.1.1's peer.context() return shape (Phase 5).

    Spec § 4 / A.0 probe: NO `messages`, `summary`, or `peer_representation`
    field. Only `peer_card: list[str]` and `representation: str | None`.
    """
    peer_card: list = field(default_factory=list)
    representation: object = None  # str | None


class StubPeerWithContext:
    """StubPeer extended with .context(target=, search_query=).

    A.0 finding: honcho-ai 2.1.1 Peer.context() does NOT accept a `tokens`
    kwarg. Stub omits it for parity with the real SDK.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.context_calls: list[dict[str, Any]] = []
        self._next_context: Any = None

    def message(self, content: str) -> StubMessage:
        return StubMessage(peer_id=self.name, content=content)

    def context(self, target, search_query):
        self.context_calls.append({
            "target": target, "search_query": search_query,
        })
        return self._next_context


class StubHonchoWithPeerContext:
    """Honcho client stub with peer.context() AND session.context() support.

    Diverges from StubHonchoCrossPeer (test_memory_cross_peer.py) in that
    `session()` is functional — peer_overlay_context tests never call it,
    but a clean stub avoids the `AssertionError` shenanigans the M6
    fixture used to prove peer-only-ness.
    """

    def __init__(self, api_key: str, base_url: str, workspace_id: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.workspace_id = workspace_id
        self.sessions: dict[str, StubSession] = {}
        self.peers: dict[str, StubPeerWithContext] = {}

    def session(self, session_id: str) -> StubSession:
        self.sessions.setdefault(session_id, StubSession(session_id))
        return self.sessions[session_id]

    def peer(self, name: str) -> StubPeerWithContext:
        self.peers.setdefault(name, StubPeerWithContext(name))
        return self.peers[name]


@pytest.fixture
def stub_env_with_peer_context(monkeypatch):
    """Patch memory module's Honcho symbol to a stub whose Peer has
    .context(). Reuses StubSessionPeerConfig from this file."""
    import memory

    monkeypatch.setattr(memory, "Honcho", StubHonchoWithPeerContext, raising=True)
    monkeypatch.setattr(
        memory, "SessionPeerConfig", StubSessionPeerConfig, raising=True,
    )
    return memory


async def test_peer_overlay_context_calls_peer_context_with_search_query(
    stub_env_with_peer_context,
):
    """Spec § 2.5: peer_overlay_context wraps peer(observer_role)
    .context(target=user_peer, search_query=q). NOTE: NO tokens kwarg
    (A.0: honcho-ai 2.1.1 doesn't accept it; capped render-side)."""
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client = p._client  # type: ignore[attr-defined]
    assistant_peer = client.peer("assistant")
    assistant_peer._next_context = StubPeerContext()

    await p.peer_overlay_context(
        observer_role="assistant",
        user_peer="nicola",
        search_query="hi",
        tokens=2000,
    )

    assert len(assistant_peer.context_calls) == 1
    call = assistant_peer.context_calls[0]
    assert call["target"] == "nicola"
    assert call["search_query"] == "hi"
    # A.0 finding: SDK doesn't accept tokens kwarg
    assert "tokens" not in call


async def test_peer_overlay_context_renders_self_perspective_headings(
    stub_env_with_peer_context,
):
    """Spec § 2.6: overlay uses self-perspective ("What I know about you"
    / "My perspective") NOT cross-role ("What X knows about you")."""
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client = p._client  # type: ignore[attr-defined]
    assistant_peer = client.peer("assistant")
    assistant_peer._next_context = StubPeerContext(
        peer_card=["lives in Amsterdam", "drinks oat milk"],
        representation="User values brevity.",
    )
    out = await p.peer_overlay_context(
        observer_role="assistant",
        user_peer="nicola",
        search_query="x",
        tokens=2000,
    )
    assert "## What I know about you" in out
    assert "## My perspective" in out
    assert "knows about you (cross-role)" not in out
    assert "- lives in Amsterdam" in out
    assert "- drinks oat milk" in out
    assert "User values brevity." in out


async def test_peer_overlay_context_returns_empty_on_honcho_error(
    stub_env_with_peer_context, caplog,
):
    """Spec § 2.5: try/except wraps SDK call; exceptions log WARNING
    and return "" — graceful-degradation parity with cross_peer_context."""
    import logging

    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client = p._client  # type: ignore[attr-defined]
    assistant_peer = client.peer("assistant")

    def _raise(target, search_query):
        raise RuntimeError("simulated Honcho 503")
    assistant_peer.context = _raise  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="memory"):
        out = await p.peer_overlay_context(
            observer_role="assistant",
            user_peer="nicola",
            search_query="x",
            tokens=2000,
        )
    assert out == ""
    assert any("peer_overlay_context" in r.message for r in caplog.records)


async def test_peer_overlay_emits_memory_call_with_call_type_self_overlay(
    stub_env_with_peer_context, caplog,
):
    """Spec § 2.9: memory_call emits call_type=self_overlay with peer-
    interpretation of fields:
        peer_count       → len(peer_card)
        summary_present  → False
        peer_repr_present → bool(representation)
        cache_hit        → False
        session_id       → "overlay-{observer_role}-{user_peer}"
    """
    import logging

    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client = p._client  # type: ignore[attr-defined]
    assistant_peer = client.peer("assistant")
    assistant_peer._next_context = StubPeerContext(
        peer_card=["a", "b", "c"],
        representation="some accumulated facts",
    )

    with caplog.at_level(logging.INFO, logger="memory"):
        await p.peer_overlay_context(
            observer_role="assistant",
            user_peer="nicola",
            search_query="x",
            tokens=2000,
        )

    records = [r for r in caplog.records if r.message == "memory_call"]
    assert len(records) == 1
    rec = records[0]
    assert rec.call_type == "self_overlay"
    assert rec.backend == "honcho"
    assert rec.session_id == "overlay-assistant-nicola"
    assert rec.agent_role == "assistant"
    assert rec.peer_count == 3
    assert rec.summary_present is False
    assert rec.peer_repr_present is True
    assert rec.cache_hit is False
