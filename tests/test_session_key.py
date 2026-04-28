"""Tests for build_session_key()."""

import pytest

from session_registry import build_session_key


def test_simple_key():
    assert build_session_key("telegram", "1197017861") == "telegram-1197017861"


def test_voice_device_key():
    assert build_session_key("voice", "kitchen-sat-01") == "voice-kitchen-sat-01"


def test_webhook_named_key():
    assert build_session_key("webhook", "deploy-notify") == "webhook-deploy-notify"


def test_int_scope_id_coerced():
    """Telegram chat_id arrives as int; must be coerced to str without error."""
    assert build_session_key("telegram", 1197017861) == "telegram-1197017861"


def test_negative_int_scope_id_coerced():
    """Telegram group chat ids are negative."""
    assert build_session_key("telegram", -1001234567890) == "telegram--1001234567890"


def test_missing_scope_id_uses_default():
    assert build_session_key("telegram", "") == "telegram-default"


def test_missing_scope_id_none_uses_default():
    assert build_session_key("telegram", None) == "telegram-default"


def test_channel_required():
    with pytest.raises(ValueError):
        build_session_key("", "1234")


def test_scope_id_with_colon_rejected():
    """The pre-fix shape preserved colons; the new builder must reject them
    so we never silently regress to producing Honcho-invalid ids again."""
    with pytest.raises(ValueError, match=r"outside \[A-Za-z0-9_-\]"):
        build_session_key("voice", "conv:abc:123")


def test_scope_id_with_whitespace_rejected():
    with pytest.raises(ValueError, match=r"outside \[A-Za-z0-9_-\]"):
        build_session_key("telegram", "with space")
