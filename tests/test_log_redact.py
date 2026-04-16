"""Tests for log_redact.py -- secret redaction."""

from log_redact import redact


class TestRedact:
    def test_redacts_sk_token(self):
        text = "Using key sk-abcdefghijklmnopqrstuvwxyz1234567890"
        result = redact(text)
        assert "sk-abcdefghijklmnopqrst" in result
        assert "uvwxyz1234567890" not in result

    def test_redacts_ghp_token(self):
        text = "token: ghp_abcd1234567890abcdef1234567890abcdef"
        result = redact(text)
        assert "ghp_abcd" in result
        assert "1234567890abcdef1234567890abcdef" not in result

    def test_redacts_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.long.token"
        result = redact(text)
        assert "Bearer ***" in result
        assert "eyJhbGci" not in result

    def test_redacts_key_value_pattern(self):
        text = 'api_key: "sk_test_abcdefghijklmnop1234567890"'
        result = redact(text)
        assert "sk_test_" in result
        assert "1234567890" not in result

    def test_preserves_normal_text(self):
        text = "This is a normal log message with no secrets"
        assert redact(text) == text

    def test_preserves_short_values(self):
        text = "token: abc"
        # Short values (< 8 chars after key) should not be redacted
        assert redact(text) == text
