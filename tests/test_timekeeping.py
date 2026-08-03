"""Tests for timekeeping.resolve_tz."""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

import pytest


def _clear_tz_env(monkeypatch):
    monkeypatch.delenv("CASA_TZ", raising=False)
    monkeypatch.delenv("TZ", raising=False)


class TestResolveTz:
    def test_defaults_to_utc_not_the_packagers_zone(self, monkeypatch):
        """Sol review: the fallback used to be Europe/Amsterdam, and the
        shipped `casa_tz` option pre-populated it — so `TZ`, which HA OS sets
        to the OPERATOR's zone, could never win. A locale-neutral fallback is
        what makes the empty option resolve to Home Assistant's own zone.
        """
        from timekeeping import resolve_tz
        resolve_tz.cache_clear()
        _clear_tz_env(monkeypatch)
        tz = resolve_tz()
        assert isinstance(tz, ZoneInfo)
        assert str(tz) == "UTC"

    def test_an_empty_casa_tz_defers_to_home_assistants_zone(self, monkeypatch):
        # The shipped option is empty, so this is the ordinary install path:
        # HA's TZ must be what takes effect.
        from timekeeping import resolve_tz
        resolve_tz.cache_clear()
        _clear_tz_env(monkeypatch)
        monkeypatch.setenv("CASA_TZ", "")
        monkeypatch.setenv("TZ", "America/New_York")
        assert str(resolve_tz()) == "America/New_York"

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

    def test_invalid_tz_falls_back_to_the_neutral_zone(self, monkeypatch, caplog):
        """Bug 6 (v0.14.6): invalid casa_tz must NOT crash every turn.

        Pre-fix: ZoneInfo() raised ZoneInfoNotFoundError, lru_cache did
        not cache the exception, so each `agent.py:_process` call
        re-raised. resolve_tz now logs and returns the neutral fallback.
        """
        import logging
        from timekeeping import resolve_tz
        resolve_tz.cache_clear()
        _clear_tz_env(monkeypatch)
        monkeypatch.setenv("CASA_TZ", "Not/A/Real/Zone")
        with caplog.at_level(logging.WARNING, logger="timekeeping"):
            tz = resolve_tz()
        assert str(tz) == "UTC"
        assert any(
            "Not/A/Real/Zone" in rec.getMessage() for rec in caplog.records
        ), f"expected a warning naming the bad TZ; got: {caplog.records}"

    def test_invalid_tz_does_not_recache_exception(self, monkeypatch):
        """Subsequent calls after a fallback also return the fallback, not
        re-raise. Guards against a regression where the fallback path is
        skipped and the exception leaks again.
        """
        from timekeeping import resolve_tz
        resolve_tz.cache_clear()
        _clear_tz_env(monkeypatch)
        monkeypatch.setenv("CASA_TZ", "Bogus/Zone")
        for _ in range(3):
            tz = resolve_tz()
            assert str(tz) == "UTC"
