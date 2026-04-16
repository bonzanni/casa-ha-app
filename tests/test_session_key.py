"""Tests for build_session_key()."""

import pytest

from session_registry import build_session_key


def test_simple_key():
    assert build_session_key("telegram", "1197017861") == "telegram:1197017861"


def test_voice_device_key():
    assert build_session_key("voice", "kitchen-sat-01") == "voice:kitchen-sat-01"


def test_webhook_named_key():
    assert build_session_key("webhook", "deploy-notify") == "webhook:deploy-notify"


def test_scoped_id_with_colons_preserved():
    """Scope IDs may contain colons; they must NOT be escaped."""
    assert build_session_key("voice", "conv:abc:123") == "voice:conv:abc:123"


def test_missing_scope_id_uses_default():
    assert build_session_key("telegram", "") == "telegram:default"


def test_missing_scope_id_none_uses_default():
    assert build_session_key("telegram", None) == "telegram:default"


def test_channel_required():
    with pytest.raises(ValueError, match="channel is required"):
        build_session_key("", "1234")
