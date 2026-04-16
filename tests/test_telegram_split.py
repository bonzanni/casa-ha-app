"""Tests for Telegram message splitting.

We import the splitting logic directly to avoid pulling in
python-telegram-bot (not installed locally).
"""

import importlib
import sys
import types
from unittest.mock import MagicMock

# Stub out the telegram package so channels.telegram can be imported
_telegram_stub = types.ModuleType("telegram")
_telegram_stub.Update = MagicMock()
_telegram_constants = types.ModuleType("telegram.constants")
_telegram_constants.ChatAction = MagicMock()
_telegram_stub.constants = _telegram_constants
_telegram_ext = types.ModuleType("telegram.ext")
_telegram_ext.Application = MagicMock()
_telegram_ext.ContextTypes = MagicMock()
_telegram_ext.MessageHandler = MagicMock()
_telegram_ext.filters = MagicMock()
_telegram_stub.ext = _telegram_ext
sys.modules.setdefault("telegram", _telegram_stub)
sys.modules.setdefault("telegram.constants", _telegram_constants)
sys.modules.setdefault("telegram.ext", _telegram_ext)

from channels.telegram import _split_message, _TG_MAX_LENGTH


class TestSplitMessage:
    def test_short_message_unchanged(self):
        result = _split_message("Hello world")
        assert result == ["Hello world"]

    def test_empty_message(self):
        result = _split_message("")
        assert result == [""]

    def test_exact_limit(self):
        text = "a" * _TG_MAX_LENGTH
        result = _split_message(text)
        assert result == [text]

    def test_splits_at_newline(self):
        # Build a message that's slightly over the limit with a newline
        first_part = "x" * (_TG_MAX_LENGTH - 10)
        second_part = "y" * 20
        text = first_part + "\n" + second_part

        result = _split_message(text)
        assert len(result) == 2
        assert result[0] == first_part
        assert result[1] == second_part

    def test_hard_split_when_no_newline(self):
        text = "a" * (_TG_MAX_LENGTH + 100)
        result = _split_message(text)
        assert len(result) == 2
        assert len(result[0]) == _TG_MAX_LENGTH
        assert len(result[1]) == 100

    def test_multiple_splits(self):
        text = "a" * (_TG_MAX_LENGTH * 3)
        result = _split_message(text)
        assert len(result) == 3

    def test_preserves_content(self):
        lines = [f"Line {i}: " + "x" * 100 for i in range(100)]
        text = "\n".join(lines)
        result = _split_message(text)
        rejoined = "\n".join(result)
        # All original content should be present
        for line in lines:
            assert line in rejoined
