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
    assert {"messages", "sessions", "peer_cards", "schema_meta"} <= tables


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
