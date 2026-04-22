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


# ---------------------------------------------------------------------------
# TestDefaultScope (3.2)
# ---------------------------------------------------------------------------


class TestDefaultScope:
    def test_default_scope_parsed(self):
        from config import MemoryConfig

        m = MemoryConfig(
            token_budget=4000,
            read_strategy="per_turn",
            scopes_owned=["personal"],
            scopes_readable=["personal", "house"],
            default_scope="personal",
        )
        assert m.default_scope == "personal"

    def test_default_scope_defaults_empty(self):
        from config import MemoryConfig

        m = MemoryConfig()
        assert m.default_scope == ""


# ---------------------------------------------------------------------------
# TestScopeValidation (3.2)
# ---------------------------------------------------------------------------


def _stub_policy_lib():
    """Minimal PolicyLibrary with a 'standard' policy for tests that
    don't care about disclosure rendering."""
    from policies import PolicyLibrary
    return PolicyLibrary({
        "standard": {
            "categories": {},
            "safe_on_any_channel": [],
            "deflection_patterns": {},
        },
    })


def _write_resident_dir(d, *, scopes_owned, scopes_readable, default_scope):
    import textwrap
    (d / "character.yaml").write_text(textwrap.dedent(f"""
        schema_version: 1
        role: {d.name}
        name: Test
        archetype: test
        card: test
        prompt: test
    """).strip() + "\n")
    (d / "voice.yaml").write_text("schema_version: 1\n")
    (d / "response_shape.yaml").write_text("schema_version: 1\n")
    (d / "disclosure.yaml").write_text(textwrap.dedent("""
        schema_version: 1
        policy: standard
        overrides: {}
    """).strip() + "\n")
    (d / "runtime.yaml").write_text(textwrap.dedent(f"""
        schema_version: 1
        model: sonnet
        channels: [telegram]
        memory:
          scopes_owned: {scopes_owned}
          scopes_readable: {scopes_readable}
          default_scope: {default_scope!r}
    """).strip() + "\n")


def _write_specialist_dir(d, *, default_scope):
    import textwrap
    (d / "character.yaml").write_text(textwrap.dedent(f"""
        schema_version: 1
        role: {d.name}
        name: Test
        archetype: test
        card: test
        prompt: test
    """).strip() + "\n")
    (d / "voice.yaml").write_text("schema_version: 1\n")
    (d / "response_shape.yaml").write_text("schema_version: 1\n")
    (d / "runtime.yaml").write_text(textwrap.dedent(f"""
        schema_version: 1
        model: sonnet
        channels: []
        memory:
          default_scope: {default_scope!r}
    """).strip() + "\n")


class TestScopeValidation:
    def test_default_scope_must_be_in_scopes_owned(self, tmp_path):
        """A resident declaring default_scope outside scopes_owned is rejected."""
        from agent_loader import load_agent_from_dir, LoadError

        agent_dir = tmp_path / "resident_bad"
        agent_dir.mkdir()
        _write_resident_dir(agent_dir, default_scope="nonexistent",
                            scopes_owned=["personal"],
                            scopes_readable=["personal", "house"])

        with pytest.raises(LoadError, match="default_scope"):
            load_agent_from_dir(str(agent_dir), policies=_stub_policy_lib())

    def test_scopes_owned_must_subset_scopes_readable(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError

        agent_dir = tmp_path / "resident_bad"
        agent_dir.mkdir()
        _write_resident_dir(agent_dir,
                            scopes_owned=["personal", "finance"],
                            scopes_readable=["personal"],  # finance missing
                            default_scope="personal")

        with pytest.raises(LoadError, match=r"scopes_owned.*subset.*scopes_readable"):
            load_agent_from_dir(str(agent_dir), policies=_stub_policy_lib())

    def test_specialist_cannot_have_default_scope(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError

        agent_dir = tmp_path / "specialist_bad"
        agent_dir.mkdir()
        _write_specialist_dir(agent_dir, default_scope="personal")

        with pytest.raises(LoadError, match="specialist.*default_scope"):
            load_agent_from_dir(str(agent_dir), policies=None)

    def test_valid_resident_loads(self, tmp_path):
        from agent_loader import load_agent_from_dir

        agent_dir = tmp_path / "resident_ok"
        agent_dir.mkdir()
        _write_resident_dir(agent_dir,
                            scopes_owned=["personal"],
                            scopes_readable=["personal", "house"],
                            default_scope="personal")

        cfg = load_agent_from_dir(str(agent_dir), policies=_stub_policy_lib())
        assert cfg.memory.default_scope == "personal"
