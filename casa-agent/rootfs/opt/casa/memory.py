"""Memory provider abstraction for Casa agents (Honcho v3 topology).

See docs/superpowers/specs/2026-04-17-honcho-v3-memory-design.md for
the peer/session model and the rationale behind the 3-method surface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class MemoryProvider(ABC):
    """Abstract memory backend.

    Five methods:
      * ``ensure_session``       — idempotent session + peer setup.
      * ``get_context``          — rendered scope-level digest (messages +
                                   summary) for the system prompt.
      * ``peer_overlay_context`` — rendered peer-level overlay (peer_card +
                                   peer representation) for the
                                   ``(observer_role, user_peer)`` pair,
                                   cross-session, semantic-filtered.
      * ``add_turn``             — persist one user→assistant turn unconditionally.
      * ``cross_peer_context``   — read another agent's accumulated representation
                                   of the user, semantic-filtered by query.

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
        tokens: int,
        search_query: str | None = None,
    ) -> str:
        """Return a rendered scope-level digest (messages + summary only).

        Empty string if the session has no relevant content yet.
        ``search_query`` is the current user utterance (used by Honcho
        for semantic retrieval). Peer-level overlay (peer_card + peer
        representation) is NOT included here — see ``peer_overlay_context``."""

    @abstractmethod
    async def peer_overlay_context(
        self,
        observer_role: str,
        user_peer: str,
        search_query: str,
        tokens: int,
    ) -> str:
        """Return a rendered peer-level overlay digest for the
        ``(observer_role, user_peer)`` pair, semantic-filtered by
        ``search_query``.

        Cross-session — Honcho's peer.context() aggregates across all
        sessions where both peers participate. Empty string when no
        representation exists yet (cold start) or on backend error
        (graceful degradation, parity with ``cross_peer_context``)."""

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

    @abstractmethod
    async def cross_peer_context(
        self,
        observer_role: str,
        query: str,
        tokens: int,
        user_peer: str = "nicola",
    ) -> str:
        """Return a rendered digest of ``observer_role``'s accumulated
        representation of ``user_peer``, semantic-filtered by ``query``.

        No session_id — Honcho's peer-level context aggregates across
        all sessions where both peers participated. Empty string when
        no representation exists yet (cold start) or on backend error
        (graceful degradation, parity with ``get_context``)."""


class NoOpMemory(MemoryProvider):
    """Stub provider when ``HONCHO_API_KEY`` is not configured."""

    async def ensure_session(
        self, session_id: str, agent_role: str, user_peer: str = "nicola",
    ) -> None:
        return None

    async def get_context(
        self,
        session_id: str,
        tokens: int,
        search_query: str | None = None,
    ) -> str:
        return ""

    async def peer_overlay_context(
        self,
        observer_role: str,
        user_peer: str,
        search_query: str,
        tokens: int,
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

    async def cross_peer_context(
        self,
        observer_role: str,
        query: str,
        tokens: int,
        user_peer: str = "nicola",
    ) -> str:
        return ""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_session(context: object) -> str:
    """Assemble a SessionContext into the markdown digest consumed by
    the system prompt. Scope-level only (messages + summary). Peer-level
    overlay (peer_card + peer_representation) belongs to
    ``_render_peer_overlay``.

    ``context`` is duck-typed: we read ``messages`` and ``summary`` if
    present. Empty/missing sections are silently omitted — no
    placeholder lines.
    """
    sections: list[str] = []

    summary = getattr(context, "summary", None)
    summary_content = getattr(summary, "content", None) if summary else None
    if summary_content:
        sections.append(f"## Summary so far\n{summary_content}")

    messages = getattr(context, "messages", None) or []
    if messages:
        lines = ["## Recent exchanges"]
        for m in messages:
            # Honcho v3 Message exposes `peer_id`; legacy _SqliteMsg uses
            # `peer_name`. Prefer the modern shape; fall back for SQLite.
            peer = getattr(m, "peer_id", None) or getattr(m, "peer_name", "?")
            lines.append(f"[{peer}] {m.content}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def _render_peer_overlay(context: object) -> str:
    """Assemble a peer.context() result (self-overlay path) into the
    markdown digest consumed by the system prompt. Peer-level only.

    Empty/missing sections are silently omitted — no placeholder lines.

    Duck-typed: reads ``peer_card`` (list[str]) and ``representation``
    (str). Mirrors ``_render_peer_context`` but with the self-perspective
    heading "What I know about you" / "My perspective" — these are
    Ellen's accumulated facts about Nicola.
    """
    sections: list[str] = []

    peer_card = getattr(context, "peer_card", None)
    if peer_card:
        lines = ["## What I know about you"]
        lines.extend(f"- {item}" for item in peer_card)
        sections.append("\n".join(lines))

    representation = getattr(context, "representation", None)
    if representation:
        sections.append(f"## My perspective\n{representation}")

    return "\n\n".join(sections)


def _render_peer_context(context: object, observer_role: str) -> str:
    """Assemble a peer.context() result into a markdown digest.

    Sections silently omitted when their source is empty —
    parity with ``_render_session``'s no-placeholder doctrine.

    Duck-typed: reads ``peer_card`` (list[str]) and ``representation``
    (str). No ``messages``, ``summary``, or ``peer_representation`` —
    those don't exist on Honcho's peer.context() return shape (spec §4).
    """
    heading = f"## What {observer_role.capitalize()} knows about you (cross-role)"
    sections: list[str] = []

    peer_card = getattr(context, "peer_card", None)
    if peer_card:
        lines = [heading]
        lines.extend(f"- {item}" for item in peer_card)
        sections.append("\n".join(lines))

    representation = getattr(context, "representation", None)
    if representation:
        if sections:
            sections.append(representation)
        else:
            sections.append(f"{heading}\n\n{representation}")

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# HonchoMemoryProvider (v3)
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402  (kept near the usage site for clarity)
import logging  # noqa: E402
import time  # noqa: E402

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
        t_start = time.perf_counter()
        session = await _to_thread(self._client.session, session_id)
        ctx = await _to_thread(
            session.context,
            tokens=tokens,
            peer_target=user_peer,
            peer_perspective=agent_role,
            search_query=search_query,
        )
        rendered = _render_session(ctx)
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
                "call_type": "self",   # M6 § 9
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

    async def cross_peer_context(
        self,
        observer_role: str,
        query: str,
        tokens: int,
        user_peer: str = "nicola",
    ) -> str:
        t_start = time.perf_counter()
        try:
            peer = await _to_thread(self._client.peer, observer_role)
            ctx = await _to_thread(
                peer.context,
                target=user_peer,
                search_query=query,
                tokens=tokens,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cross_peer_context failed for observer=%r user_peer=%r: %s",
                observer_role, user_peer, exc,
            )
            # No memory_call emission on error — operator sees the
            # WARNING line; double-emitting on errors would falsify
            # backend-success-rate dashboards.
            return ""
        rendered = _render_peer_context(ctx, observer_role)
        # M3b/M6 telemetry — companion line on every successful Honcho
        # backend call. Field reinterpretation per spec § 9:
        #   peer_count       → len(peer_card) (no messages on peer.context)
        #   summary_present  → False (no summary surface)
        #   peer_repr_present → bool(representation) (semantic map)
        #   cache_hit        → False (no caching for cross-peer)
        #   call_type        → "cross_peer"
        #   session_id       → synthetic peer-{observer_role}-{user_peer}
        #                      (peer-level reads aren't bound to a session;
        #                      the deterministic synthetic value keeps the
        #                      log parser uniform — `peer-` prefix can't
        #                      collide with real 4-segment session ids).
        logger.info(
            "memory_call",
            extra={
                "backend": "honcho",
                "session_id": f"peer-{observer_role}-{user_peer}",
                "agent_role": observer_role,
                "t_ms": int((time.perf_counter() - t_start) * 1000),
                "peer_count": len(getattr(ctx, "peer_card", None) or []),
                "summary_present": False,
                "peer_repr_present": bool(getattr(ctx, "representation", None)),
                "cache_hit": False,
                "call_type": "cross_peer",
            },
        )
        return rendered


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

    @staticmethod
    def _resolve_backend_name(backend: "MemoryProvider") -> str:
        """Short string for memory_call's `backend` field.

        Lookup table for the three production provider classes; falls
        back to `class.__name__` lowercased + suffix-stripped for tests
        and any future provider. Used only by the M3b telemetry line on
        the cache-hit path — production providers emit their own name
        directly from get_context.
        """
        # Use class-name string (not isinstance) so the table works
        # under the lazy-import pattern at module load time.
        cls = backend.__class__.__name__
        table = {
            "HonchoMemoryProvider": "honcho",
            "SqliteMemoryProvider": "sqlite",
            "NoOpMemory": "noop",
        }
        if cls in table:
            return table[cls]
        # Fallback: strip "MemoryProvider" or "Provider" suffix,
        # lowercase the rest. `RecordingProvider` (test stub) →
        # "recording".
        if cls.endswith("MemoryProvider"):
            return cls[:-len("MemoryProvider")].lower()
        if cls.endswith("Provider"):
            return cls[:-len("Provider")].lower()
        return cls.lower()

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
        t_start = time.perf_counter()
        key = (session_id, agent_role, tokens)
        cached = self._cache.get(key)
        if cached is not None:
            # M3b — wrapper emits its own line on cache hit since the
            # inner backend never runs. peer_count / summary_present /
            # peer_repr_present are None because the cache stores only
            # the rendered string; re-deriving from the string would be
            # brittle (header parsing). Operators get cache_hit=True as
            # the primary signal.
            logger.info(
                "memory_call",
                extra={
                    "backend": self._resolve_backend_name(self._backend),
                    "session_id": session_id,
                    "agent_role": agent_role,
                    "t_ms": int((time.perf_counter() - t_start) * 1000),
                    "peer_count": None,
                    "summary_present": None,
                    "peer_repr_present": None,
                    "cache_hit": True,
                    "call_type": "self",   # M6 § 9
                },
            )
            return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)  # re-check after acquire
            if cached is not None:
                # Miss + recheck-hit fast path (concurrent caller filled
                # the cache while we waited on the lock). Same emission
                # as the top-of-method hit branch.
                logger.info(
                    "memory_call",
                    extra={
                        "backend": self._resolve_backend_name(self._backend),
                        "session_id": session_id,
                        "agent_role": agent_role,
                        "t_ms": int((time.perf_counter() - t_start) * 1000),
                        "peer_count": None,
                        "summary_present": None,
                        "peer_repr_present": None,
                        "cache_hit": True,
                        "call_type": "self",   # M6 § 9
                    },
                )
                return cached
            fresh = await self._backend.get_context(
                session_id, agent_role, tokens,
                search_query=search_query, user_peer=user_peer,
            )
            self._cache[key] = fresh
        # Cache miss path — inner backend's get_context already emitted
        # its own memory_call line with cache_hit=False. We do NOT emit
        # here; double-emission would falsify rate dashboards.
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

    async def cross_peer_context(
        self,
        observer_role: str,
        query: str,
        tokens: int,
        user_peer: str = "nicola",
    ) -> str:
        # Spec M6 § 5.2: passthrough, no caching. The cache today
        # exists for voice latency on get_context; cross-peer reads
        # happen on text-channel turns at low volume per turn, and a
        # correct cache key would have to include `query` — defeating
        # the cache's purpose. Same shape as ensure_session and
        # add_turn passthroughs above.
        return await self._backend.cross_peer_context(
            observer_role, query, tokens, user_peer,
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
    """Duck-typed shape consumed by ``_render_session``.

    SQLite emits messages-only digests (no Honcho summary surface, no
    peer-level data). Graceful-degradation contract per
    docs/superpowers/specs/2026-04-26-memory-architecture.md §10.
    """
    messages: list[_SqliteMsg] = field(default_factory=list)
    summary: None = None


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
        self, session_id: str, tokens: int,
        search_query: str | None = None,
    ) -> str:
        t_start = time.perf_counter()
        # search_query is ignored on SQLite (no semantic retrieval).
        rendered, peer_count = await asyncio.to_thread(
            self._get_context_sync, session_id, tokens,
        )
        # M3b telemetry — backend=sqlite always emits summary_present /
        # peer_repr_present False per the spec § 10 graceful-degradation
        # contract.
        logger.info(
            "memory_call",
            extra={
                "backend": "sqlite",
                "session_id": session_id,
                "agent_role": "?",
                "t_ms": int((time.perf_counter() - t_start) * 1000),
                "peer_count": peer_count,
                "summary_present": False,
                "peer_repr_present": False,
                "cache_hit": False,
                "call_type": "self",   # M6 § 9
            },
        )
        return rendered

    def _get_context_sync(
        self, session_id: str, tokens: int,
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
        return _render_session(_SqliteCtx(messages=messages)), len(messages)

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

    async def cross_peer_context(
        self,
        observer_role: str,
        query: str,
        tokens: int,
        user_peer: str = "nicola",
    ) -> str:
        # Spec § 10 graceful-degradation contract: SQLite has no
        # representation surface; cross-peer recall returns "" so
        # callers consistently see "no memory" rather than a partial
        # last-N digest that wouldn't be peer-perspective-attributed.
        return ""

    async def peer_overlay_context(
        self,
        observer_role: str,
        user_peer: str,
        search_query: str,
        tokens: int,
    ) -> str:
        # Spec § 2.5 graceful-degradation contract: SQLite has no peer-level
        # primitive (peer_card is a Honcho-deriver feature). Return "" so
        # callers consistently see "no overlay" rather than partial or
        # mis-attributed content.
        return ""
