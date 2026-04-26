"""Unit tests for SqliteMemoryProvider against in-memory DBs."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

pytestmark = pytest.mark.asyncio


# --- Schema / init ----------------------------------------------------------


async def test_open_creates_all_tables():
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    tables = {
        row[0]
        for row in p._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    # M1.C: peer_cards dropped — Honcho is where peer cards live.
    assert {"messages", "sessions", "schema_meta"} <= tables
    assert "peer_cards" not in tables


async def test_schema_version_seeded():
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    row = p._conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    assert row is not None
    assert row[0] == "1"


async def test_pragmas_applied_on_open():
    """journal_mode=WAL, synchronous=NORMAL (1), foreign_keys=ON (1),
    busy_timeout=5000. Exception: ``:memory:`` DBs ignore journal_mode=WAL
    and silently stay on MEMORY — the other three PRAGMAs still apply."""
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    sync = p._conn.execute("PRAGMA synchronous").fetchone()[0]
    fk = p._conn.execute("PRAGMA foreign_keys").fetchone()[0]
    timeout = p._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert sync == 1          # NORMAL
    assert fk == 1            # ON
    assert timeout == 5000


async def test_journal_mode_wal_on_disk(tmp_path):
    """journal_mode=WAL survives the round-trip on a real file."""
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(str(tmp_path / "mem.sqlite"))
    mode = p._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


async def test_reopen_existing_db_preserves_data_and_schema(tmp_path):
    """Re-opening an existing file must not clobber rows, and
    schema_version must not be re-seeded (INSERT OR IGNORE)."""
    from memory import SqliteMemoryProvider

    path = str(tmp_path / "mem.sqlite")
    p1 = SqliteMemoryProvider(path)
    # Seed a row via raw SQL — Task 1 does not yet implement add_turn.
    p1._conn.execute(
        "INSERT INTO messages (session_id, peer_name, content, ts) "
        "VALUES (?, ?, ?, ?)",
        ("s1", "nicola", "hello", 1.0),
    )
    p1._conn.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
        ("1",),
    )
    p1._conn.commit()
    p1._conn.close()

    p2 = SqliteMemoryProvider(path)
    row = p2._conn.execute(
        "SELECT content FROM messages WHERE session_id = 's1'"
    ).fetchone()
    assert row is not None
    assert row[0] == "hello"

    version = p2._conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == "1"

    # All four tables must still be present (sanity).
    tables = {
        r[0] for r in p2._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"messages", "sessions", "peer_cards", "schema_meta"} <= tables


async def test_parent_directory_created(tmp_path):
    """Missing parent dir is created, not an error."""
    from memory import SqliteMemoryProvider

    nested = tmp_path / "nested" / "deeper" / "mem.sqlite"
    assert not nested.parent.exists()
    SqliteMemoryProvider(str(nested))
    assert nested.parent.is_dir()


# --- ensure_session ---------------------------------------------------------


async def test_ensure_session_inserts_first_time():
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("telegram:1:assistant", "assistant")

    row = p._conn.execute(
        "SELECT agent_role, user_peer, created_ts, last_active "
        "FROM sessions WHERE session_id=?",
        ("telegram:1:assistant",),
    ).fetchone()
    assert row is not None
    assert row[0] == "assistant"
    assert row[1] == "nicola"
    assert row[2] == row[3]  # just-inserted, created_ts == last_active


async def test_ensure_session_updates_last_active_on_second_call():
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("s", "assistant")
    first = p._conn.execute(
        "SELECT created_ts, last_active FROM sessions WHERE session_id='s'",
    ).fetchone()

    await asyncio.sleep(0.01)
    await p.ensure_session("s", "assistant")
    second = p._conn.execute(
        "SELECT created_ts, last_active FROM sessions WHERE session_id='s'",
    ).fetchone()

    assert second[0] == first[0]           # created_ts unchanged
    assert second[1] > first[1]            # last_active bumped


async def test_ensure_session_preserves_topology_across_calls():
    """Topology (agent_role, user_peer) is set by the first creator.
    A caller passing different values later does not overwrite it."""
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("voice:lr:butler", "butler", user_peer="voice_speaker")
    # Second call with wrong-looking args — must not rewrite.
    await p.ensure_session("voice:lr:butler", "assistant", user_peer="nicola")

    row = p._conn.execute(
        "SELECT agent_role, user_peer FROM sessions WHERE session_id=?",
        ("voice:lr:butler",),
    ).fetchone()
    assert row[0] == "butler"
    assert row[1] == "voice_speaker"


# --- add_turn --------------------------------------------------------------


async def test_add_turn_writes_user_and_assistant_rows():
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("telegram:1:assistant", "assistant")
    await p.add_turn(
        "telegram:1:assistant", "assistant",
        user_text="hi", assistant_text="hello",
    )

    rows = p._conn.execute(
        "SELECT peer_name, content FROM messages "
        "WHERE session_id=? ORDER BY id ASC",
        ("telegram:1:assistant",),
    ).fetchall()
    assert rows == [("nicola", "hi"), ("assistant", "hello")]


async def test_add_turn_voice_uses_voice_speaker():
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("voice:lr:butler", "butler", user_peer="voice_speaker")
    await p.add_turn(
        "voice:lr:butler", "butler",
        user_text="lights on", assistant_text="ok",
        user_peer="voice_speaker",
    )

    rows = p._conn.execute(
        "SELECT peer_name FROM messages "
        "WHERE session_id=? ORDER BY id ASC",
        ("voice:lr:butler",),
    ).fetchall()
    assert [r[0] for r in rows] == ["voice_speaker", "butler"]


async def test_add_turn_bumps_last_active():
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("s", "assistant")
    before = p._conn.execute(
        "SELECT last_active FROM sessions WHERE session_id='s'",
    ).fetchone()[0]

    await asyncio.sleep(0.02)
    await p.add_turn("s", "assistant", "u", "a")
    after = p._conn.execute(
        "SELECT last_active FROM sessions WHERE session_id='s'",
    ).fetchone()[0]
    assert after > before


async def test_add_turn_is_transactional_on_failure():
    """Mid-transaction failure must leave zero rows, not one."""
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("s", "assistant")

    real_conn = p._conn
    call_count = {"n": 0}

    class FlakyConn:
        """Proxy that injects an error on the 2nd INSERT INTO messages."""

        def __enter__(self):
            return real_conn.__enter__()

        def __exit__(self, *exc):
            return real_conn.__exit__(*exc)

        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().upper().startswith("INSERT INTO MESSAGES"):
                call_count["n"] += 1
                if call_count["n"] == 2:
                    raise sqlite3.OperationalError(
                        "simulated failure on 2nd insert"
                    )
            return real_conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    p._conn = FlakyConn()
    try:
        with pytest.raises(sqlite3.OperationalError):
            await p.add_turn("s", "assistant", "u", "a")
    finally:
        p._conn = real_conn

    rows = real_conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id='s'",
    ).fetchone()[0]
    assert rows == 0


async def test_add_turn_ordering_preserved_on_identical_ts(monkeypatch):
    """Rapid-fire add_turns with the same ts must still sort by id."""
    from memory import SqliteMemoryProvider
    import time as builtin_time

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("s", "assistant")

    fixed = builtin_time.time()
    # memory.py imports time at function scope inside _add_turn_sync, so
    # patching time.time on the time module itself is what takes effect.
    monkeypatch.setattr(builtin_time, "time", lambda: fixed)

    await p.add_turn("s", "assistant", "u1", "a1")
    await p.add_turn("s", "assistant", "u2", "a2")

    rows = p._conn.execute(
        "SELECT content FROM messages WHERE session_id='s' ORDER BY id ASC",
    ).fetchall()
    assert [r[0] for r in rows] == ["u1", "a1", "u2", "a2"]


# --- get_context -----------------------------------------------------------


async def test_get_context_empty_returns_empty_string():
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("s", "assistant")
    out = await p.get_context("s", "assistant", tokens=4000)
    assert out == ""


async def test_get_context_renders_recent_exchanges_chronologically():
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("s", "assistant")
    await p.add_turn("s", "assistant", "q1", "a1")
    await p.add_turn("s", "assistant", "q2", "a2")

    out = await p.get_context("s", "assistant", tokens=4000)
    assert "## Recent exchanges" in out
    # Oldest first after chronological reverse.
    q1_pos = out.index("[nicola] q1")
    a2_pos = out.index("[assistant] a2")
    assert q1_pos < a2_pos


async def test_get_context_truncates_by_token_budget():
    """tokens=40 → last_n = max(1, 40//40) = 1 row. One add_turn writes
    two rows (user + assistant), so LIMIT 1 returns only the newest
    (assistant) row."""
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("s", "assistant")
    await p.add_turn("s", "assistant", "q1", "a1")
    out = await p.get_context("s", "assistant", tokens=40)
    assert "[assistant] a1" in out
    assert "[nicola] q1" not in out


async def test_get_context_minimum_one_row_when_budget_is_zero():
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("s", "assistant")
    await p.add_turn("s", "assistant", "q1", "a1")
    out = await p.get_context("s", "assistant", tokens=0)
    # max(1, 0) = 1 — we still see the most recent row.
    assert "[assistant] a1" in out


async def test_get_context_search_query_ignored():
    """Passing a search_query must not alter the output — SQLite has no
    semantic retrieval (spec §6 / S9)."""
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("s", "assistant")
    await p.add_turn("s", "assistant", "q1", "a1")

    plain = await p.get_context("s", "assistant", tokens=4000)
    with_query = await p.get_context(
        "s", "assistant", tokens=4000, search_query="anything",
    )
    assert plain == with_query


async def test_get_context_no_summary_or_perspective_sections():
    """SQLite never emits ``## Summary so far`` or ``## My perspective``
    (spec §5)."""
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("s", "assistant")
    await p.add_turn("s", "assistant", "q1", "a1")
    out = await p.get_context("s", "assistant", tokens=4000)
    assert "## Summary so far" not in out
    assert "## My perspective" not in out


# --- Topology visibility smoke --------------------------------------------


async def test_butler_session_never_sees_assistant_session_messages():
    """A butler session's get_context must only see its own messages,
    even though both sessions live in the same DB (spec §6)."""
    from memory import SqliteMemoryProvider

    p = SqliteMemoryProvider(":memory:")
    await p.ensure_session("telegram:1:assistant", "assistant")
    await p.add_turn(
        "telegram:1:assistant", "assistant",
        "secret from telegram", "telegram answer",
    )

    await p.ensure_session("voice:lr:butler", "butler", user_peer="voice_speaker")
    await p.add_turn(
        "voice:lr:butler", "butler", "voice q", "voice a",
        user_peer="voice_speaker",
    )

    out = await p.get_context(
        "voice:lr:butler", "butler", tokens=4000, user_peer="voice_speaker",
    )
    assert "[voice_speaker] voice q" in out
    assert "[butler] voice a" in out
    assert "secret from telegram" not in out
    assert "telegram answer" not in out
