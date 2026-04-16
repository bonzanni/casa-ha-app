"""Tests for config.py -- model mapping and agent config loading."""

import textwrap

import pytest

from config import (
    AgentConfig,
    MemoryConfig,
    SessionConfig,
    ToolsConfig,
    load_agent_config,
    resolve_model,
)


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
# load_agent_config
# ------------------------------------------------------------------


class TestLoadAgentConfig:
    def test_load_all_fields(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            name: TestBot
            role: tester
            model: sonnet
            personality: "Friendly bot"
            description: "A test bot"
            tools:
              allowed: [Read, Write]
              disallowed: [Bash]
              permission_mode: acceptEdits
              max_turns: 15
            mcp_server_names:
              - homeassistant
            memory:
              peer_name: test-peer
              token_budget: 5000
              exclude_tags: [private]
            session:
              strategy: persistent
              idle_timeout: 600
            channels:
              - telegram
              - webhook
            cwd: /workspace
        """)
        cfg_file = tmp_path / "agent.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")

        cfg = load_agent_config(str(cfg_file))

        assert isinstance(cfg, AgentConfig)
        assert cfg.name == "TestBot"
        assert cfg.role == "tester"
        assert cfg.model == "claude-sonnet-4-6"
        assert cfg.personality == "Friendly bot"
        assert cfg.description == "A test bot"

        assert isinstance(cfg.tools, ToolsConfig)
        assert cfg.tools.allowed == ["Read", "Write"]
        assert cfg.tools.disallowed == ["Bash"]
        assert cfg.tools.permission_mode == "acceptEdits"
        assert cfg.tools.max_turns == 15

        assert cfg.mcp_server_names == ["homeassistant"]

        assert isinstance(cfg.memory, MemoryConfig)
        assert cfg.memory.peer_name == "test-peer"
        assert cfg.memory.token_budget == 5000
        assert cfg.memory.exclude_tags == ["private"]

        assert isinstance(cfg.session, SessionConfig)
        assert cfg.session.strategy == "persistent"
        assert cfg.session.idle_timeout == 600

        assert cfg.channels == ["telegram", "webhook"]
        assert cfg.cwd == "/workspace"

    def test_env_substitution(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PRIMARY_AGENT_NAME", "Ellen")
        monkeypatch.setenv("PRIMARY_AGENT_MODEL", "opus")

        yaml_content = textwrap.dedent("""\
            name: "${PRIMARY_AGENT_NAME}"
            role: main
            model: "${PRIMARY_AGENT_MODEL}"
            personality: "I am ${PRIMARY_AGENT_NAME}."
        """)
        cfg_file = tmp_path / "agent.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")

        cfg = load_agent_config(str(cfg_file))

        assert cfg.name == "Ellen"
        assert cfg.model == "claude-opus-4-6"
        assert "Ellen" in cfg.personality


def test_missing_role_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "name: Test\n"
        "model: opus\n"
        "personality: hi\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Missing required 'role' field"):
        load_agent_config(str(p))


def test_legacy_main_role_normalized(tmp_path, caplog):
    import logging

    p = tmp_path / "legacy.yaml"
    p.write_text(
        "name: Test\n"
        "role: main\n"
        "model: opus\n"
        "personality: hi\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        cfg = load_agent_config(str(p))
    assert cfg.role == "assistant"
    assert any("deprecated" in r.message.lower() for r in caplog.records)


def test_role_assistant_accepted(tmp_path):
    p = tmp_path / "new.yaml"
    p.write_text(
        "name: Test\n"
        "role: assistant\n"
        "model: opus\n"
        "personality: hi\n",
        encoding="utf-8",
    )
    cfg = load_agent_config(str(p))
    assert cfg.role == "assistant"
