"""Tests for log_redact.py -- secret redaction."""

import pytest

from log_redact import redact

# Without this marker the tier2 unit gate (-m "unit and not docker and not
# slow") silently skips this whole file.
pytestmark = pytest.mark.unit


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


class TestRedactAnthropicKeys:
    """M19 (v0.50.0): Casa's own primary key format (sk-ant-...) must be
    redacted. Pre-fix the sk- pattern required 20 contiguous alphanumerics
    after 'sk-', which the hyphen after 'ant' broke, so the key passed
    through logs unredacted."""

    def test_redacts_anthropic_api_key(self):
        secret_body = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-AA"
        text = f"spawned claude with sk-ant-api03-{secret_body}"
        result = redact(text)
        assert secret_body not in result
        assert "sk-ant-api03-" in result  # prefix retained for identification

    def test_redacts_anthropic_oauth_token(self):
        # mirrors sdk_logging.py's verbatim CLI-stderr echo path
        secret_body = "AbCd_EfGh-IjKlMnOpQrStUv"
        text = f"stderr Error: invalid credential sk-ant-oat01-{secret_body}"
        result = redact(text)
        assert secret_body not in result
        assert "sk-ant-oat01-" in result
