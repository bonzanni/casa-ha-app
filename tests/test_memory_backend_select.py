"""Tests for casa_core.resolve_memory_backend_choice — spec §2."""

from __future__ import annotations

import pytest


def test_unset_env_no_key_defaults_to_sqlite():
    from casa_core import resolve_memory_backend_choice

    choice = resolve_memory_backend_choice(env={})
    assert choice.backend == "sqlite"
    assert choice.db_path == "/data/memory.sqlite"


def test_unset_env_with_key_uses_honcho():
    from casa_core import resolve_memory_backend_choice

    choice = resolve_memory_backend_choice(env={"HONCHO_API_KEY": "abc"})
    assert choice.backend == "honcho"
    assert choice.honcho_api_key == "abc"
    assert choice.honcho_api_url == "https://api.honcho.dev"


def test_explicit_sqlite_wins_even_with_honcho_key():
    from casa_core import resolve_memory_backend_choice

    choice = resolve_memory_backend_choice(env={
        "MEMORY_BACKEND": "sqlite",
        "HONCHO_API_KEY": "abc",
    })
    assert choice.backend == "sqlite"


def test_explicit_honcho_without_key_raises():
    from casa_core import resolve_memory_backend_choice

    with pytest.raises(ValueError, match="HONCHO_API_KEY"):
        resolve_memory_backend_choice(env={"MEMORY_BACKEND": "honcho"})


def test_explicit_honcho_with_key_uses_honcho():
    from casa_core import resolve_memory_backend_choice

    choice = resolve_memory_backend_choice(env={
        "MEMORY_BACKEND": "honcho",
        "HONCHO_API_KEY": "abc",
        "HONCHO_API_URL": "https://honcho.example",
    })
    assert choice.backend == "honcho"
    assert choice.honcho_api_url == "https://honcho.example"


def test_explicit_noop_returns_noop():
    from casa_core import resolve_memory_backend_choice

    choice = resolve_memory_backend_choice(env={"MEMORY_BACKEND": "noop"})
    assert choice.backend == "noop"


def test_invalid_memory_backend_raises():
    from casa_core import resolve_memory_backend_choice

    with pytest.raises(ValueError, match="MEMORY_BACKEND"):
        resolve_memory_backend_choice(env={"MEMORY_BACKEND": "telepathy"})


def test_memory_db_path_override_honoured():
    from casa_core import resolve_memory_backend_choice

    choice = resolve_memory_backend_choice(env={
        "MEMORY_BACKEND": "sqlite",
        "MEMORY_DB_PATH": "/tmp/other.sqlite",
    })
    assert choice.db_path == "/tmp/other.sqlite"


# --- CachedMemoryProvider wrap policy (spec §2) -----------------------------


def test_wrap_for_strategy_cached_on_honcho_returns_cached(caplog):
    import logging

    from casa_core import _wrap_memory_for_strategy
    from memory import CachedMemoryProvider, NoOpMemory

    # Any non-SQLite backend exercises the Cached-wrap path.
    stand_in = NoOpMemory()

    with caplog.at_level(logging.INFO):
        wrapped = _wrap_memory_for_strategy(
            stand_in, role="butler", strategy="cached",
            sqlite_warning_emitted=[False],
        )
    assert isinstance(wrapped, CachedMemoryProvider)


def test_wrap_for_strategy_cached_on_sqlite_returns_bare_and_logs(caplog):
    import logging

    from casa_core import _wrap_memory_for_strategy
    from memory import SqliteMemoryProvider

    backend = SqliteMemoryProvider(":memory:")
    flag = [False]
    with caplog.at_level(logging.INFO):
        wrapped = _wrap_memory_for_strategy(
            backend, role="butler", strategy="cached",
            sqlite_warning_emitted=flag,
        )
    assert wrapped is backend
    assert flag[0] is True
    assert any("SQLite" in r.message and "caching" in r.message
               for r in caplog.records)


def test_wrap_for_strategy_cached_on_sqlite_log_only_once(caplog):
    import logging

    from casa_core import _wrap_memory_for_strategy
    from memory import SqliteMemoryProvider

    backend = SqliteMemoryProvider(":memory:")
    flag = [False]
    with caplog.at_level(logging.INFO):
        _wrap_memory_for_strategy(
            backend, "butler", "cached", sqlite_warning_emitted=flag,
        )
        _wrap_memory_for_strategy(
            backend, "other", "cached", sqlite_warning_emitted=flag,
        )
    sqlite_lines = [
        r for r in caplog.records
        if "SQLite" in r.message and "caching" in r.message
    ]
    assert len(sqlite_lines) == 1


def test_wrap_for_strategy_per_turn_returns_bare():
    from casa_core import _wrap_memory_for_strategy
    from memory import NoOpMemory

    backend = NoOpMemory()
    flag = [False]
    wrapped = _wrap_memory_for_strategy(
        backend, "assistant", "per_turn", sqlite_warning_emitted=flag,
    )
    assert wrapped is backend


def test_wrap_for_strategy_card_only_falls_back_to_per_turn_with_warning(caplog):
    import logging

    from casa_core import _wrap_memory_for_strategy
    from memory import NoOpMemory

    backend = NoOpMemory()
    flag = [False]
    with caplog.at_level(logging.WARNING):
        wrapped = _wrap_memory_for_strategy(
            backend, "finance", "card_only", sqlite_warning_emitted=flag,
        )
    assert wrapped is backend
    assert any("card_only" in r.message for r in caplog.records)
