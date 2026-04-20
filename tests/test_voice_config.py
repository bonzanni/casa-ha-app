"""Tests for the TTSConfig dataclass.

Voice-loading tests (voice_errors / TTS via the agent directory) live in
``tests/test_agent_loader.py``; this module only exercises the dataclass
validation that lives in ``config.py``.
"""

import pytest

from config import TTSConfig


class TestTTSConfig:
    def test_defaults(self):
        cfg = TTSConfig()
        assert cfg.tag_dialect == "square_brackets"

    def test_valid_dialects(self):
        for dialect in ("square_brackets", "parens", "none"):
            TTSConfig(tag_dialect=dialect)  # no raise

    def test_invalid_dialect_rejected(self):
        with pytest.raises(ValueError, match="tag_dialect"):
            TTSConfig(tag_dialect="ssml")
