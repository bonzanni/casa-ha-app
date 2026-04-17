"""Memory provider abstraction for Casa agents (Honcho v3 topology).

See docs/superpowers/specs/2026-04-17-honcho-v3-memory-design.md for
the peer/session model and the rationale behind the 3-method surface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class MemoryProvider(ABC):
    """Abstract memory backend.

    Three methods:
      * ``ensure_session`` — idempotent session + peer setup.
      * ``get_context``    — rendered memory digest for the system prompt.
      * ``add_turn``       — persist one user→assistant turn unconditionally.

    Storage is never filtered; write-scoping is a structural property of
    ``session_id`` + peer topology (spec §4.3). Disclosure decisions
    happen on the output side, not here.
    """

    @abstractmethod
    async def ensure_session(
        self,
        session_id: str,
        agent_role: str,
        user_peer: str = "nicola",
    ) -> None:
        """Ensure the session exists with peers ``[user_peer, agent_role]``
        and ``observe_others=True`` on ``agent_role``.

        Cheap when already set up. Safe to call every turn."""

    @abstractmethod
    async def get_context(
        self,
        session_id: str,
        agent_role: str,
        tokens: int,
        search_query: str | None = None,
        user_peer: str = "nicola",
    ) -> str:
        """Return a rendered memory digest for the system prompt.

        Empty string if the session has no relevant content yet.
        ``search_query`` is the current user utterance (used by Honcho
        for semantic retrieval). ``user_peer`` is the peer whose
        perspective to target."""

    @abstractmethod
    async def add_turn(
        self,
        session_id: str,
        agent_role: str,
        user_text: str,
        assistant_text: str,
        user_peer: str = "nicola",
    ) -> None:
        """Persist one user→assistant turn. ``user_text`` → ``user_peer``;
        ``assistant_text`` → ``agent_role``. Never filtered."""


class NoOpMemory(MemoryProvider):
    """Stub provider when ``HONCHO_API_KEY`` is not configured."""

    async def ensure_session(
        self, session_id: str, agent_role: str, user_peer: str = "nicola",
    ) -> None:
        return None

    async def get_context(
        self,
        session_id: str,
        agent_role: str,
        tokens: int,
        search_query: str | None = None,
        user_peer: str = "nicola",
    ) -> str:
        return ""

    async def add_turn(
        self,
        session_id: str,
        agent_role: str,
        user_text: str,
        assistant_text: str,
        user_peer: str = "nicola",
    ) -> None:
        return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(context: object) -> str:
    """Assemble a SessionContext into the markdown digest consumed by the
    system prompt. Empty/missing sections are silently omitted — no
    placeholder lines.

    ``context`` is duck-typed: we read ``messages``, ``summary``,
    ``peer_representation``, ``peer_card`` if present. Keeps the test
    surface decoupled from the Honcho SDK types.
    """
    sections: list[str] = []

    peer_card = getattr(context, "peer_card", None)
    if peer_card:
        lines = ["## What I know about you"]
        lines.extend(f"- {item}" for item in peer_card)
        sections.append("\n".join(lines))

    summary = getattr(context, "summary", None)
    summary_content = getattr(summary, "content", None) if summary else None
    if summary_content:
        sections.append(f"## Summary so far\n{summary_content}")

    peer_repr = getattr(context, "peer_representation", None)
    if peer_repr:
        sections.append(f"## My perspective\n{peer_repr}")

    messages = getattr(context, "messages", None) or []
    if messages:
        lines = ["## Recent exchanges"]
        for m in messages:
            lines.append(f"[{m.peer_name}] {m.content}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# HonchoMemoryProvider (v3)
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402  (kept near the usage site for clarity)
import logging  # noqa: E402

logger = logging.getLogger(__name__)

# Lazy-import so import of this module does not fail when honcho-ai is
# absent (e.g. a test run without the dep). The NoOpMemory path needs
# nothing. Both symbols are resolved at class-instantiation time, so
# tests can monkeypatch them on the module.
try:
    from honcho import Honcho  # type: ignore[import-untyped]
    from honcho.api_types import SessionPeerConfig  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — exercised in NoOpMemory-only installs
    Honcho = None  # type: ignore[assignment]
    SessionPeerConfig = None  # type: ignore[assignment]


async def _to_thread(func, /, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


class HonchoMemoryProvider(MemoryProvider):
    """Honcho v3 backed memory provider.

    All SDK calls are offloaded to a thread. Peers and sessions are
    lazy in v3 — no API calls fire until a method that hits the server
    is invoked.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        workspace_id: str = "casa",
    ) -> None:
        if Honcho is None:
            raise RuntimeError(
                "honcho-ai is not installed; cannot construct "
                "HonchoMemoryProvider. Install honcho-ai>=2.1.1."
            )
        self._client = Honcho(
            api_key=api_key, base_url=api_url, workspace_id=workspace_id,
        )

    async def ensure_session(
        self,
        session_id: str,
        agent_role: str,
        user_peer: str = "nicola",
    ) -> None:
        session = await _to_thread(self._client.session, session_id)
        user = await _to_thread(self._client.peer, user_peer)
        agent = await _to_thread(self._client.peer, agent_role)
        await _to_thread(
            session.add_peers,
            [
                (user, SessionPeerConfig(
                    observe_me=True, observe_others=False,
                )),
                (agent, SessionPeerConfig(
                    observe_me=False, observe_others=True,
                )),
            ],
        )

    async def get_context(
        self,
        session_id: str,
        agent_role: str,
        tokens: int,
        search_query: str | None = None,
        user_peer: str = "nicola",
    ) -> str:
        session = await _to_thread(self._client.session, session_id)
        ctx = await _to_thread(
            session.context,
            tokens=tokens,
            peer_target=user_peer,
            peer_perspective=agent_role,
            search_query=search_query,
        )
        return _render(ctx)

    async def add_turn(
        self,
        session_id: str,
        agent_role: str,
        user_text: str,
        assistant_text: str,
        user_peer: str = "nicola",
    ) -> None:
        session = await _to_thread(self._client.session, session_id)
        user = await _to_thread(self._client.peer, user_peer)
        agent = await _to_thread(self._client.peer, agent_role)
        await _to_thread(session.add_messages, [
            user.message(user_text),
            agent.message(assistant_text),
        ])
