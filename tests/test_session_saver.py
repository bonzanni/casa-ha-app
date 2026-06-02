# tests/test_session_saver.py
"""Per-channel freshness windows (spec §3.3): voice short, telegram long."""
from __future__ import annotations

from datetime import timedelta

import pytest

from session_saver import freshness_window

pytestmark = [pytest.mark.unit]


def test_voice_is_short():
    assert freshness_window("voice") == timedelta(minutes=30)


def test_telegram_is_long():
    assert freshness_window("telegram") == timedelta(hours=12)


def test_unknown_channel_falls_back_to_telegram_default():
    assert freshness_window("something-else") == timedelta(hours=12)


def test_env_override(monkeypatch):
    monkeypatch.setenv("FRESHNESS_VOICE_MINUTES", "10")
    assert freshness_window("voice") == timedelta(minutes=10)
