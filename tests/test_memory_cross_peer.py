"""Unit tests for HonchoMemoryProvider.cross_peer_context (M6).

Mirrors the StubHoncho/StubSession/StubPeer pattern used by
test_memory_honcho.py — extended with a `peer.context()` method that
returns a separate shape from `session.context()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


# --- Stubs for peer.context() — different shape from session.context ---


@dataclass
class StubPeerContext:
    """Mirror of honcho-ai 2.1.1's peer.context() return shape.

    Spec § 4 confirms there is NO `messages`, `summary`, or
    `peer_representation` field. Only `peer_card: list[str]` and
    `representation: str | None`.
    """
    peer_card: list = field(default_factory=list)
    representation: object = None  # str | None


class StubPeerWithContext:
    """StubPeer extended with .context(target=, search_query=, tokens=)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.context_calls: list[dict[str, Any]] = []
        self._next_context: Any = None

    def message(self, content: str):  # parity with StubPeer
        from test_memory_honcho import StubMessage  # local import OK
        return StubMessage(peer_name=self.name, content=content)

    def context(self, target, search_query, tokens):
        self.context_calls.append({
            "target": target, "search_query": search_query, "tokens": tokens,
        })
        return self._next_context


class StubHonchoCrossPeer:
    """Honcho client stub with peer.context() support."""

    def __init__(self, api_key: str, base_url: str, workspace_id: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.workspace_id = workspace_id
        self.peers: dict[str, StubPeerWithContext] = {}

    def session(self, session_id: str):  # not used by cross_peer_context
        raise AssertionError(
            "cross_peer_context must NOT call session() — it's peer-level"
        )

    def peer(self, name: str) -> StubPeerWithContext:
        self.peers.setdefault(name, StubPeerWithContext(name))
        return self.peers[name]


@pytest.fixture
def stub_env(monkeypatch):
    import memory
    monkeypatch.setattr(memory, "Honcho", StubHonchoCrossPeer, raising=True)
    # SessionPeerConfig not used by cross_peer_context but keep the patch
    # for parity with the existing test_memory_honcho.py fixture.
    from test_memory_honcho import StubSessionPeerConfig
    monkeypatch.setattr(memory, "SessionPeerConfig", StubSessionPeerConfig, raising=True)
    return memory


# --- Tests ----------------------------------------------------------------


async def test_cross_peer_context_calls_peer_context_with_search_query(stub_env):
    """Spec § 5.2 + § 4: peer(observer_role).context(target=user_peer,
    search_query=query, tokens=tokens) is the canonical call shape."""
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client = p._client  # type: ignore[attr-defined]

    # Prime an empty context — we only check the call shape here
    finance_peer = client.peer("finance")
    finance_peer._next_context = StubPeerContext()

    await p.cross_peer_context(
        observer_role="finance",
        query="what does Finance know about my budget",
        tokens=2000,
    )

    assert len(finance_peer.context_calls) == 1
    call = finance_peer.context_calls[0]
    assert call["target"] == "nicola"
    assert call["search_query"] == "what does Finance know about my budget"
    assert call["tokens"] == 2000


async def test_cross_peer_context_returns_empty_when_honcho_returns_empty(stub_env):
    """Empty peer.context() return → "" (cold-start parity)."""
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client = p._client  # type: ignore[attr-defined]
    client.peer("finance")._next_context = StubPeerContext()

    out = await p.cross_peer_context(
        observer_role="finance", query="anything", tokens=2000,
    )
    assert out == ""


async def test_cross_peer_context_renders_peer_card_and_representation(stub_env):
    """Populated SDK return → both sections render."""
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client = p._client  # type: ignore[attr-defined]
    client.peer("finance")._next_context = StubPeerContext(
        peer_card=["prioritizes Q2 invoicing", "ENPICOM is primary client"],
        representation=(
            "User has explicitly asked Finance to handle outstanding "
            "invoices via the Q2 batch."
        ),
    )

    out = await p.cross_peer_context(
        observer_role="finance",
        query="what does Finance know about my priorities",
        tokens=2000,
    )

    assert "## What Finance knows about you (cross-role)" in out
    assert "- prioritizes Q2 invoicing" in out
    assert "- ENPICOM is primary client" in out
    assert "User has explicitly asked Finance" in out


async def test_cross_peer_context_returns_empty_on_honcho_error(stub_env, caplog):
    """Spec § 5.2: try/except wraps SDK call; exceptions log WARNING
    and return "" — graceful-degradation parity with get_context."""
    import logging

    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client = p._client  # type: ignore[attr-defined]

    finance_peer = client.peer("finance")

    def _raise(target, search_query, tokens):
        raise RuntimeError("simulated Honcho 503")
    finance_peer.context = _raise  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="memory"):
        out = await p.cross_peer_context(
            observer_role="finance", query="x", tokens=2000,
        )

    assert out == ""
    assert any("503" in r.message or "cross_peer_context" in r.message
               for r in caplog.records)


async def test_cross_peer_context_does_not_call_session(stub_env):
    """Spec § 5.1: peer-level primitive — no session_id, no session()
    call. The stub raises if session() is invoked, so a passing test
    proves the impl is peer-level."""
    from memory import HonchoMemoryProvider

    p = HonchoMemoryProvider(api_url="http://h", api_key="k")
    client = p._client  # type: ignore[attr-defined]
    client.peer("finance")._next_context = StubPeerContext()

    # If the impl ever calls client.session(...), the stub raises
    # AssertionError. A clean return means peer-level only.
    out = await p.cross_peer_context(
        observer_role="finance", query="x", tokens=2000,
    )
    assert out == ""
