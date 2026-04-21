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

    def test_invalid_tz_raises(self, monkeypatch):
        from timekeeping import resolve_tz
        from zoneinfo import ZoneInfoNotFoundError
        resolve_tz.cache_clear()
        _clear_tz_env(monkeypatch)
        monkeypatch.setenv("CASA_TZ", "Not/A/Real/Zone")
        with pytest.raises(ZoneInfoNotFoundError):
            resolve_tz()
