"""Tests for channel_trust.py — canonical token + display helper."""


class TestChannelTrust:
    def test_telegram_returns_authenticated(self):
        from channel_trust import channel_trust
        assert channel_trust("telegram") == "authenticated"

    def test_voice_returns_household_shared(self):
        from channel_trust import channel_trust
        assert channel_trust("voice") == "household-shared"

    def test_webhook_returns_authenticated(self):
        # X2 (2026-07-10): /invoke + /webhook are HMAC-gated; operator decision
        # "the secret is the trust boundary" → trusted like the authenticated DM
        # so the agent may disclose private categories (meets disclosure.yaml's
        # required_trust: authenticated).
        from channel_trust import channel_trust
        assert channel_trust("webhook") == "authenticated"

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
        assert channel_trust_display("webhook") == "authenticated (shared secret)"

    def test_display_unknown_channel_falls_back(self):
        from channel_trust import channel_trust_display
        assert channel_trust_display("mystery") == "unknown"


class TestUserPeerMovedOut:
    """#204: per-turn AUTHOR identity left this module for ingress_identity.

    The retired ``user_peer_for_channel`` defaulted to ``"nicola"`` for any
    channel absent from its map, so /invoke and /webhook would have recorded
    third-party content as authored by the operator. Peers are now declared per
    ingress ROUTE, with no default at all — trust (this module) and authorship
    (ingress_identity) are separate axes.
    """

    def test_channel_trust_no_longer_owns_peer_identity(self):
        import channel_trust
        assert not hasattr(channel_trust, "user_peer_for_channel")

    def test_telegram_peer_now_comes_from_the_ingress_table(self):
        from ingress_identity import ingress_identity
        assert ingress_identity("telegram").user_peer == "nicola"

    def test_voice_peer_now_comes_from_the_ingress_table(self):
        from ingress_identity import ingress_identity
        assert ingress_identity("voice_sse").user_peer == "voice_speaker"
