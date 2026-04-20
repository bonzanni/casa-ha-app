"""Tests for config.py -- model mapping and dataclasses."""

import pytest

from config import resolve_model


# ------------------------------------------------------------------
# resolve_model
# ------------------------------------------------------------------


class TestResolveModel:
    def test_shortname_opus(self):
        assert resolve_model("opus") == "claude-opus-4-6"

    def test_shortname_sonnet(self):
        assert resolve_model("sonnet") == "claude-sonnet-4-6"

    def test_shortname_haiku(self):
        assert resolve_model("haiku") == "claude-haiku-4-5"

    def test_passthrough_full_id(self):
        assert resolve_model("claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_passthrough_custom_full_id(self):
        assert resolve_model("my-custom-model-3") == "my-custom-model-3"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown model shortname"):
            resolve_model("gpt4")


# ------------------------------------------------------------------
# Dataclasses (Phase 4.x agent-definition refactor)
# ------------------------------------------------------------------


class TestCharacterConfig:
    def test_defaults(self):
        from config import CharacterConfig
        cfg = CharacterConfig(
            name="X", archetype="y", card="c", prompt="p",
        )
        assert cfg.name == "X"
        assert cfg.archetype == "y"
        assert cfg.card == "c"
        assert cfg.prompt == "p"


class TestVoiceConfig:
    def test_defaults(self):
        from config import VoiceConfig
        cfg = VoiceConfig()
        assert cfg.tone == []
        assert cfg.cadence == "natural"
        assert cfg.forbidden_patterns == []
        assert cfg.signature_phrases == {}


class TestResponseShapeConfig:
    def test_defaults(self):
        from config import ResponseShapeConfig
        cfg = ResponseShapeConfig()
        assert cfg.max_sentences_confirmation == 2
        assert cfg.max_sentences_status == 3
        assert cfg.register == "written"
        assert cfg.format == "plain"
        assert cfg.rules == []


class TestDisclosureConfig:
    def test_defaults(self):
        from config import DisclosureConfig
        cfg = DisclosureConfig(policy="standard")
        assert cfg.policy == "standard"
        assert cfg.overrides == {}


class TestDelegateEntry:
    def test_fields(self):
        from config import DelegateEntry
        e = DelegateEntry(agent="finance", purpose="p", when="w")
        assert e.agent == "finance"


class TestTriggerSpec:
    def test_interval(self):
        from config import TriggerSpec
        t = TriggerSpec(name="hb", type="interval",
                        minutes=60, channel="telegram", prompt="x")
        assert t.type == "interval"
        assert t.minutes == 60

    def test_cron(self):
        from config import TriggerSpec
        t = TriggerSpec(name="morning", type="cron",
                        schedule="0 7 * * *", channel="telegram", prompt="x")
        assert t.type == "cron"

    def test_webhook(self):
        from config import TriggerSpec
        t = TriggerSpec(name="gh", type="webhook", path="/webhook/gh")
        assert t.type == "webhook"
        assert t.path == "/webhook/gh"


class TestHooksConfig:
    def test_defaults(self):
        from config import HooksConfig
        h = HooksConfig()
        assert h.pre_tool_use == []


class TestAgentConfigNewFields:
    def test_new_fields_default_to_empty(self):
        from config import (
            AgentConfig, CharacterConfig, VoiceConfig,
            ResponseShapeConfig, HooksConfig,
        )
        cfg = AgentConfig()
        assert isinstance(cfg.character, CharacterConfig)
        assert isinstance(cfg.voice, VoiceConfig)
        assert isinstance(cfg.response_shape, ResponseShapeConfig)
        assert cfg.disclosure is None
        assert cfg.delegates == []
        assert cfg.triggers == []
        assert isinstance(cfg.hooks, HooksConfig)
        assert cfg.system_prompt == ""
