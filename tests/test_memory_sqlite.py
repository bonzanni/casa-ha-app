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
