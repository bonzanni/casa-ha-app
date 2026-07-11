"""Tests for agent error classification.

The classification logic is pure Python; ``claude_agent_sdk`` is installed
in the test venv, so we import ``agent`` directly rather than stubbing a
stale SDK surface (the old hand-maintained stub drifted from agent.py's
imports and only survived on full-gate import ordering).
"""

import asyncio

from agent import ErrorKind, _classify_error, _USER_MESSAGES


class TestClassifyError:
    def test_timeout_error(self):
        assert _classify_error(asyncio.TimeoutError()) == ErrorKind.TIMEOUT

    def test_rate_limit_message(self):
        exc = Exception("Rate limit exceeded, retry after 60s")
        assert _classify_error(exc) == ErrorKind.RATE_LIMIT

    def test_429_status(self):
        exc = Exception("HTTP 429: Too Many Requests")
        assert _classify_error(exc) == ErrorKind.RATE_LIMIT

    def test_timeout_in_message(self):
        exc = Exception("Connection timed out after 30s")
        assert _classify_error(exc) == ErrorKind.TIMEOUT

    def test_cli_error(self):
        class CLIConnectionError(Exception):
            pass

        assert _classify_error(CLIConnectionError("not connected")) == ErrorKind.SDK_ERROR

    def test_sdk_error(self):
        class SDKError(Exception):
            pass

        assert _classify_error(SDKError("bad state")) == ErrorKind.SDK_ERROR

    def test_unknown_fallback(self):
        assert _classify_error(ValueError("something odd")) == ErrorKind.UNKNOWN

    def test_overloaded_error_is_retryable(self):
        """An Anthropic 529 overload surfaces from the SDK as a ``ProcessError``
        (type name lacks CLI/SDK/Connection) whose message carries neither
        'rate limit', '429', nor 'timeout' — pre-fix it fell through to
        UNKNOWN and was never retried, the single most common transient
        failure. Classify as RATE_LIMIT so the backoff loop handles it."""
        class ProcessError(Exception):
            pass

        exc = ProcessError(
            "Command failed with exit code 1: "
            '{"type":"error","error":{"type":"overloaded_error",'
            '"message":"Overloaded"}}'
        )
        assert _classify_error(exc) == ErrorKind.RATE_LIMIT

    def test_http_529_is_retryable(self):
        exc = Exception("API error: HTTP 529 Overloaded")
        assert _classify_error(exc) == ErrorKind.RATE_LIMIT

    def test_overloaded_is_in_retry_kinds(self):
        """End-to-end: the classification must land in the retry set."""
        from retry import RETRY_KINDS

        class ProcessError(Exception):
            pass

        exc = ProcessError("stderr: overloaded_error — Overloaded")
        assert _classify_error(exc) in RETRY_KINDS

    def test_all_kinds_have_user_messages(self):
        for kind in ErrorKind:
            assert kind in _USER_MESSAGES
            assert len(_USER_MESSAGES[kind]) > 0
