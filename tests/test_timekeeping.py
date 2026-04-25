"""Tests for timekeeping.resolve_tz."""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

import pytest


def _clear_tz_env(monkeypatch):
    monkeypatch.delenv("CASA_TZ", raising=False)
    monkeypatch.delenv("TZ", raising=False)


class TestResolveTz:
    def test_defaults_to_europe_amsterdam(self, monkeypatch):
        from timekeeping import resolve_tz
        resolve_tz.cache_clear()
        _clear_tz_env(monkeypatch)
        tz = resolve_tz()
        assert isinstance(tz, ZoneInfo)
        assert str(tz) == "Europe/Amsterdam"

    def test_casa_tz_env_wins(self, monkeypatch):
        from timekeeping import resolve_tz
        resolve_tz.cache_clear()
        _clear_tz_env(monkeypatch)
        monkeypatch.setenv("CASA_TZ", "America/New_York")
        monkeypatch.setenv("TZ", "UTC")
        tz = resolve_tz()
        assert str(tz) == "America/New_York"

    def test_tz_env_fallback(self, monkeypatch):
        from timekeeping import resolve_tz
        resolve_tz.cache_clear()
        _clear_tz_env(monkeypatch)
        monkeypatch.setenv("TZ", "UTC")
        tz = resolve_tz()
        assert str(tz) == "UTC"

    def test_invalid_tz_falls_back_to_amsterdam(self, monkeypatch, caplog):
        """Bug 6 (v0.14.6): invalid casa_tz must NOT crash every turn.

        Pre-fix: ZoneInfo() raised ZoneInfoNotFoundError, lru_cache did
        not cache the exception, so each `agent.py:_process` call
        re-raised. resolve_tz now logs and returns Europe/Amsterdam.
        """
        import logging
        from timekeeping import resolve_tz
        resolve_tz.cache_clear()
        _clear_tz_env(monkeypatch)
        monkeypatch.setenv("CASA_TZ", "Not/A/Real/Zone")
        with caplog.at_level(logging.WARNING, logger="timekeeping"):
            tz = resolve_tz()
        assert str(tz) == "Europe/Amsterdam"
        assert any(
            "Not/A/Real/Zone" in rec.getMessage() for rec in caplog.records
        ), f"expected a warning naming the bad TZ; got: {caplog.records}"

    def test_invalid_tz_does_not_recache_exception(self, monkeypatch):
        """Subsequent calls after a fallback also return Amsterdam, not
        re-raise. Guards against a regression where the fallback path is
        skipped and the exception leaks again.
        """
        from timekeeping import resolve_tz
        resolve_tz.cache_clear()
        _clear_tz_env(monkeypatch)
        monkeypatch.setenv("CASA_TZ", "Bogus/Zone")
        for _ in range(3):
            tz = resolve_tz()
            assert str(tz) == "Europe/Amsterdam"
