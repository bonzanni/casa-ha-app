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
