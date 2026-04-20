"""Tests for channel_trust.py — canonical token + display helper."""


class TestChannelTrust:
    def test_telegram_returns_authenticated(self):
        from channel_trust import channel_trust
        assert channel_trust("telegram") == "authenticated"

    def test_voice_returns_household_shared(self):
        from channel_trust import channel_trust
        assert channel_trust("voice") == "household-shared"

    def test_webhook_returns_external_authenticated(self):
        from channel_trust import channel_trust
        assert channel_trust("webhook") == "external-authenticated"

    def test_scheduler_returns_internal(self):
        from channel_trust import channel_trust
        assert channel_trust("scheduler") == "internal"

    def test_unknown_channel_returns_public(self):
        from channel_trust import channel_trust
        assert channel_trust("mystery") == "public"


class TestChannelTrustDisplay:
    def test_display_returns_human_readable(self):
        from channel_trust import channel_trust_display
        assert channel_trust_display("telegram") == "authenticated (Nicola)"
        assert channel_trust_display("voice") == "household-shared (speaker unauthenticated)"
        assert channel_trust_display("scheduler") == "internal (system-initiated)"
        assert channel_trust_display("webhook") == "external (authenticated by shared secret)"

    def test_display_unknown_channel_falls_back(self):
        from channel_trust import channel_trust_display
        assert channel_trust_display("mystery") == "unknown"


class TestUserPeer:
    def test_telegram_user_peer_is_nicola(self):
        from channel_trust import user_peer_for_channel
        assert user_peer_for_channel("telegram") == "nicola"

    def test_voice_user_peer_is_voice_speaker(self):
        from channel_trust import user_peer_for_channel
        assert user_peer_for_channel("voice") == "voice_speaker"
