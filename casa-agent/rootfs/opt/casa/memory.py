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
        import time
        t_start = time.perf_counter()
        session = await _to_thread(self._client.session, session_id)
        ctx = await _to_thread(
            session.context,
            tokens=tokens,
            peer_target=user_peer,
            peer_perspective=agent_role,
            search_query=search_query,
        )
        rendered = _render(ctx)
        # M3b telemetry — one line per real backend call. peer_count is
        # message count; summary_present / peer_repr_present are bools
        # derived from the SDK return shape so operators can see WHEN
        # Honcho actually populates these fields without re-running the
        # render.
        summary_obj = getattr(ctx, "summary", None)
        logger.info(
            "memory_call",
            extra={
                "backend": "honcho",
                "session_id": session_id,
                "agent_role": agent_role,
                "t_ms": int((time.perf_counter() - t_start) * 1000),
                "peer_count": len(getattr(ctx, "messages", None) or []),
                "summary_present": bool(getattr(summary_obj, "content", None)),
                "peer_repr_present": bool(getattr(ctx, "peer_representation", None)),
                "cache_hit": False,
            },
        )
        return rendered

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


# ---------------------------------------------------------------------------
# CachedMemoryProvider (voice latency strategy S1)
# ---------------------------------------------------------------------------


class CachedMemoryProvider(MemoryProvider):
    """Warm-cache + background-refresh wrapper around a concrete provider.

    First call for a given ``(session_id, agent_role, tokens)`` key
    fetches synchronously and caches the rendered string. Subsequent
    calls return the cached value immediately. ``add_turn`` writes
    through and fires a background refresh so the next read already
    reflects the new turn — without blocking the caller.

    ``search_query`` is intentionally not part of the cache key: the
    cache is for low-latency paths (butler voice), which trade
    per-turn semantic retrieval for speed (spec §7 S1).
    """

    def __init__(self, backend: "MemoryProvider") -> None:
        self._backend = backend
        self._cache: dict[tuple[str, str, int], str] = {}
        self._locks: dict[tuple[str, str, int], asyncio.Lock] = {}
        self._bg_tasks: set[asyncio.Task] = set()

    async def ensure_session(
        self, session_id: str, agent_role: str, user_peer: str = "nicola",
    ) -> None:
        await self._backend.ensure_session(session_id, agent_role, user_peer)

    async def get_context(
        self,
        session_id: str,
        agent_role: str,
        tokens: int,
        search_query: str | None = None,
        user_peer: str = "nicola",
    ) -> str:
        key = (session_id, agent_role, tokens)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)  # re-check after acquire
            if cached is not None:
                return cached
            fresh = await self._backend.get_context(
                session_id, agent_role, tokens,
                search_query=search_query, user_peer=user_peer,
            )
            self._cache[key] = fresh
        return fresh

    async def add_turn(
        self,
        session_id: str,
        agent_role: str,
        user_text: str,
        assistant_text: str,
        user_peer: str = "nicola",
    ) -> None:
        await self._backend.add_turn(
            session_id, agent_role, user_text, assistant_text, user_peer,
        )
        task = asyncio.create_task(
            self._refresh(session_id, agent_role, user_peer),
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _refresh(
        self, session_id: str, agent_role: str, user_peer: str,
    ) -> None:
        # Refresh every (session_id, agent_role, tokens) entry we've
        # already cached. Token-budget variations are uncommon in
        # practice but we keep the cache coherent either way.
        keys = [k for k in self._cache if k[0] == session_id
                                       and k[1] == agent_role]
        for key in keys:
            _, _, tokens = key
            try:
                fresh = await self._backend.get_context(
                    session_id, agent_role, tokens,
                    search_query=None, user_peer=user_peer,
                )
                self._cache[key] = fresh
            except Exception as exc:
                logger.warning(
                    "CachedMemoryProvider refresh failed for %s/%s: %s",
                    session_id, agent_role, exc,
                )


# ---------------------------------------------------------------------------
# SqliteMemoryProvider (spec: 2026-04-17-sqlite-memory-2.2b)
# ---------------------------------------------------------------------------

import os  # noqa: E402
import sqlite3  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    peer_name   TEXT NOT NULL,
    content     TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session_id_id
    ON messages(session_id, id DESC);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    agent_role   TEXT NOT NULL,
    user_peer    TEXT NOT NULL,
    created_ts   REAL NOT NULL,
    last_active  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


@dataclass
class _SqliteMsg:
    peer_name: str
    content: str


@dataclass
class _SqliteCtx:
    """Duck-typed shape consumed by ``_render``.

    ``peer_card``, ``summary`` and ``peer_representation`` are always
    absent on SQLite — graceful-degradation contract (M1.C, see
    docs/superpowers/specs/2026-04-26-memory-architecture.md §10).
    Honcho's SessionContext supplies them.
    """
    messages: list[_SqliteMsg] = field(default_factory=list)
    summary: None = None
    peer_representation: None = None


class SqliteMemoryProvider(MemoryProvider):
    """Durable local-storage memory backend.

    Single connection held for process lifetime (spec §3). ``sqlite3``
    is synchronous — every call is wrapped in ``asyncio.to_thread``.
    ``check_same_thread=False`` lets the connection be reused from the
    thread-pool; SQLite's own locking + ``busy_timeout=5000`` covers
    any transient collision.
    """

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._apply_pragmas()
        self._conn.executescript(_SQLITE_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", "1"),
        )
        self._conn.commit()

    def _apply_pragmas(self) -> None:
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")

    # Method stubs — filled in by Tasks 2–4.
    async def ensure_session(
        self, session_id: str, agent_role: str, user_peer: str = "nicola",
    ) -> None:
        await asyncio.to_thread(
            self._ensure_session_sync, session_id, agent_role, user_peer,
        )

    def _ensure_session_sync(
        self, session_id: str, agent_role: str, user_peer: str,
    ) -> None:
        import time
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions "
                "(session_id, agent_role, user_peer, created_ts, last_active) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, agent_role, user_peer, now, now),
            )
            self._conn.execute(
                "UPDATE sessions SET last_active = ? WHERE session_id = ?",
                (now, session_id),
            )

    async def get_context(
        self, session_id: str, agent_role: str, tokens: int,
        search_query: str | None = None, user_peer: str = "nicola",
    ) -> str:
        import time
        t_start = time.perf_counter()
        # search_query is ignored on SQLite (no semantic retrieval).
        rendered, peer_count = await asyncio.to_thread(
            self._get_context_sync, session_id, tokens, user_peer,
        )
        # M3b telemetry — backend=sqlite always emits summary_present /
        # peer_repr_present False per the spec § 10 graceful-degradation
        # contract.
        logger.info(
            "memory_call",
            extra={
                "backend": "sqlite",
                "session_id": session_id,
                "agent_role": agent_role,
                "t_ms": int((time.perf_counter() - t_start) * 1000),
                "peer_count": peer_count,
                "summary_present": False,
                "peer_repr_present": False,
                "cache_hit": False,
            },
        )
        return rendered

    def _get_context_sync(
        self, session_id: str, tokens: int, user_peer: str,
    ) -> tuple[str, int]:
        last_n = max(1, tokens // 40)
        msg_rows = self._conn.execute(
            "SELECT peer_name, content FROM messages "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, last_n),
        ).fetchall()
        messages = [
            _SqliteMsg(peer_name=r[0], content=r[1])
            for r in reversed(msg_rows)
        ]
        return _render(_SqliteCtx(messages=messages)), len(messages)

    async def add_turn(
        self, session_id: str, agent_role: str,
        user_text: str, assistant_text: str, user_peer: str = "nicola",
    ) -> None:
        await asyncio.to_thread(
            self._add_turn_sync,
            session_id, agent_role, user_text, assistant_text, user_peer,
        )

    def _add_turn_sync(
        self, session_id: str, agent_role: str,
        user_text: str, assistant_text: str, user_peer: str,
    ) -> None:
        import time
        now = time.time()
        with self._conn:  # rolls back automatically on exception
            self._conn.execute(
                "INSERT INTO messages "
                "(session_id, peer_name, content, ts) VALUES (?, ?, ?, ?)",
                (session_id, user_peer, user_text, now),
            )
            self._conn.execute(
                "INSERT INTO messages "
                "(session_id, peer_name, content, ts) VALUES (?, ?, ?, ?)",
                (session_id, agent_role, assistant_text, now),
            )
            self._conn.execute(
                "UPDATE sessions SET last_active = ? WHERE session_id = ?",
                (now, session_id),
            )
