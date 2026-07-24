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


class TestExactValueRegistration:
    """Release A (Task 6): per-trigger webhook secrets are opaque and match no
    generic pattern, so they are registered for exact-value redaction."""

    def setup_method(self):
        from log_redact import _reset_registered_secrets
        _reset_registered_secrets()

    def teardown_method(self):
        from log_redact import _reset_registered_secrets
        _reset_registered_secrets()

    def test_registered_value_is_redacted(self):
        from log_redact import register_secret
        register_secret("whsec_opaqueProviderValue123")
        out = redact("delivering with secret whsec_opaqueProviderValue123 now")
        assert "whsec_opaqueProviderValue123" not in out
        assert "delivering with secret" in out

    def test_unregistered_value_untouched(self):
        out = redact("plain harmless text 12345")
        assert "plain harmless text 12345" in out

    def test_short_or_empty_values_not_registered(self):
        from log_redact import register_secret
        register_secret("")      # ignored — would blank everything
        register_secret("abc")   # too short to be a meaningful secret
        out = redact("abc and more")
        assert "abc and more" in out


class TestRedactingFilterArgs:
    """#214: python-telegram-bot logs Bot API call parameters as a DICT
    positional arg at DEBUG (``telegram._bot`` line 732: ``"Calling Bot API
    endpoint `%s` with parameters `%s`", endpoint, data``). The setWebhook
    ``secret_token`` rides inside that non-string ``data`` dict, which the
    filter's per-arg redaction skipped (only top-level ``str`` args were
    redacted) and which redaction never saw because it ran before %-format.
    The filter must redact secrets embedded in ANY arg (dict/list/str)."""

    def setup_method(self):
        from log_redact import _reset_registered_secrets
        _reset_registered_secrets()

    def teardown_method(self):
        from log_redact import _reset_registered_secrets
        _reset_registered_secrets()

    def _emit(self, record):
        import logging as _logging
        from log_redact import RedactingFilter
        assert RedactingFilter().filter(record) is True
        # Mirror what a handler's formatter does — this is what actually
        # reaches stdout, and the secret must be gone from it.
        return _logging.Formatter("%(message)s").format(record)

    def _record(self, msg, args):
        import logging as _logging
        return _logging.LogRecord(
            name="telegram.Bot", level=_logging.DEBUG,
            pathname=__file__, lineno=1, msg=msg, args=args, exc_info=None,
        )

    def test_registered_secret_in_dict_arg_redacted(self):
        from log_redact import register_secret
        register_secret("AAsecretTokenValue12345")
        record = self._record(
            "Calling Bot API endpoint `%s` with parameters `%s`",
            ("setWebhook",
             {"url": "https://x/y", "secret_token": "AAsecretTokenValue12345"}),
        )
        out = self._emit(record)
        assert "AAsecretTokenValue12345" not in out
        assert "setWebhook" in out  # non-secret context preserved

    def test_pattern_secret_in_dict_arg_redacted(self):
        record = self._record(
            "params `%s`",
            ({"secret_token": "sk-abcdefghijklmnopqrstuvwxyz1234567890"},),
        )
        out = self._emit(record)
        assert "uvwxyz1234567890" not in out

    def test_plain_string_arg_still_redacted(self):
        from log_redact import register_secret
        register_secret("whsec_opaqueProviderValue123")
        record = self._record("delivering %s now", ("whsec_opaqueProviderValue123",))
        out = self._emit(record)
        assert "whsec_opaqueProviderValue123" not in out
        assert "delivering" in out and "now" in out

    def test_no_args_message_redacted(self):
        record = self._record(
            "token: ghp_abcd1234567890abcdef1234567890abcdef", None)
        out = self._emit(record)
        assert "1234567890abcdef1234567890abcdef" not in out

    def test_short_secret_token_masked_by_key_name(self):
        # Sol review: a short secret_token is < the 8-char registration floor
        # and matches no generic pattern; key-name masking is what guarantees
        # it can't leak from PTB's parameter dict.
        record = self._record(
            "Calling Bot API endpoint `%s` with parameters `%s`",
            ("setWebhook", {"url": "https://x/y", "secret_token": "sh0rt"}),
        )
        out = self._emit(record)
        assert "sh0rt" not in out
        assert "setWebhook" in out and "https://x/y" in out

    def test_benign_key_label_not_over_redacted(self):
        # The bare word "key" is a common non-secret dict key (cache/routing);
        # it must survive so logs stay useful.
        record = self._record(
            "%s", ({"key": "voice-latency", "resume": "False"},))
        out = self._emit(record)
        assert "voice-latency" in out
