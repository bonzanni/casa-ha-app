"""Tests for voice-related config loading (TTSConfig, voice_errors)."""

import textwrap

import pytest

from config import AgentConfig, TTSConfig, load_agent_config


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


class TestLoadVoiceFields:
    def _write(self, tmp_path, body):
        p = tmp_path / "butler.yaml"
        p.write_text(textwrap.dedent(body), encoding="utf-8")
        return str(p)

    def test_tts_block_loaded(self, tmp_path):
        path = self._write(tmp_path, """\
            name: Tina
            role: butler
            model: haiku
            tts:
              tag_dialect: parens
        """)
        cfg = load_agent_config(path)
        assert cfg.tts.tag_dialect == "parens"

    def test_tts_absent_defaults_to_square_brackets(self, tmp_path):
        path = self._write(tmp_path, """\
            name: Tina
            role: butler
            model: haiku
        """)
        cfg = load_agent_config(path)
        assert cfg.tts.tag_dialect == "square_brackets"

    def test_voice_errors_loaded_as_dict(self, tmp_path):
        path = self._write(tmp_path, """\
            name: Tina
            role: butler
            model: haiku
            voice_errors:
              timeout: "[apologetic] Too slow."
              unknown: "[flat] Broken."
        """)
        cfg = load_agent_config(path)
        assert cfg.voice_errors["timeout"] == "[apologetic] Too slow."
        assert cfg.voice_errors["unknown"] == "[flat] Broken."

    def test_voice_errors_absent_is_empty_dict(self, tmp_path):
        path = self._write(tmp_path, """\
            name: Tina
            role: butler
            model: haiku
        """)
        cfg = load_agent_config(path)
        assert cfg.voice_errors == {}
