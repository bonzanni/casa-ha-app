"""Tests for channel_trust helpers."""

import pytest

from channel_trust import channel_trust, user_peer_for_channel


class TestUserPeerForChannel:
    def test_telegram_is_nicola(self):
        assert user_peer_for_channel("telegram") == "nicola"

    def test_webhook_is_nicola(self):
        assert user_peer_for_channel("webhook") == "nicola"

    def test_scheduler_is_nicola(self):
        assert user_peer_for_channel("scheduler") == "nicola"

    def test_voice_is_voice_speaker(self):
        assert user_peer_for_channel("voice") == "voice_speaker"

    def test_unknown_channel_defaults_to_nicola(self):
        assert user_peer_for_channel("imap") == "nicola"


class TestChannelTrust:
    def test_telegram(self):
        assert channel_trust("telegram") == "authenticated (Nicola)"

    def test_voice(self):
        assert channel_trust("voice") == "household-shared (speaker unauthenticated)"

    def test_scheduler(self):
        assert channel_trust("scheduler") == "internal (system-initiated)"

    def test_webhook(self):
        assert channel_trust("webhook") == "external (authenticated by shared secret)"

    def test_unknown(self):
        assert channel_trust("imap") == "unknown"
