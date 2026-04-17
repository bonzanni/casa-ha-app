"""Unit tests for SqliteMemoryProvider against in-memory DBs."""

from __future__ import annotations

import asyncio
import os
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


async def test_reopen_existing_db_is_idempotent(tmp_path):
    """Re-opening an existing file does not clobber rows."""
    from memory import SqliteMemoryProvider

    path = str(tmp_path / "mem.sqlite")
    p1 = SqliteMemoryProvider(path)
    await p1.ensure_session("s1", "assistant")
    p1._conn.close()

    p2 = SqliteMemoryProvider(path)
    row = p2._conn.execute(
        "SELECT session_id FROM sessions WHERE session_id='s1'"
    ).fetchone()
    assert row is not None


async def test_parent_directory_created(tmp_path):
    """Missing parent dir is created, not an error."""
    from memory import SqliteMemoryProvider

    nested = tmp_path / "nested" / "deeper" / "mem.sqlite"
    assert not nested.parent.exists()
    SqliteMemoryProvider(str(nested))
    assert nested.parent.is_dir()
