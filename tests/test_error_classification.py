"""Tests for agent error classification.

We stub out claude_agent_sdk to avoid importing the SDK (not installed
locally). The error classification logic is pure Python.
"""

import asyncio
import sys
import types
from unittest.mock import MagicMock

# Stub out claude_agent_sdk before importing agent
_sdk_stub = types.ModuleType("claude_agent_sdk")
for _name in [
    "AssistantMessage",
    "ClaudeAgentOptions",
    "ClaudeSDKClient",
    "HookMatcher",
    "ResultMessage",
    "SystemMessage",
    "TextBlock",
    "create_sdk_mcp_server",
    "tool",
]:
    setattr(_sdk_stub, _name, MagicMock())
sys.modules.setdefault("claude_agent_sdk", _sdk_stub)

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

    def test_all_kinds_have_user_messages(self):
        for kind in ErrorKind:
            assert kind in _USER_MESSAGES
            assert len(_USER_MESSAGES[kind]) > 0
