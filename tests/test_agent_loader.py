"""Tests for agent_loader.py — per-agent-dir loader, schema validation,
composition, tier-specific file-set rules."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _seed_resident(base: Path, role: str = "assistant") -> Path:
    """Write a minimal valid resident directory. Returns the agent dir path."""
    d = base / role
    _w(d / "character.yaml", f"""\
        schema_version: 1
        name: Ellen
        role: {role}
        archetype: assistant
        card: |
          Peer summary.
        prompt: |
          You are Ellen.
    """)
    _w(d / "voice.yaml", """\
        schema_version: 1
        tone: [direct]
        cadence: short
    """)
    _w(d / "response_shape.yaml", """\
        schema_version: 1
        register: written
        format: plain
    """)
    _w(d / "disclosure.yaml", """\
        schema_version: 1
        policy: standard
    """)
    _w(d / "runtime.yaml", """\
        schema_version: 1
        model: sonnet
        tools:
          allowed: [Read, Write]
        channels: [telegram]
    """)
    return d


def _seed_executor(base: Path, role: str = "finance") -> Path:
    d = base / role
    _w(d / "character.yaml", f"""\
        schema_version: 1
        name: Alex
        role: {role}
        archetype: finance
        card: |
          Peer card.
        prompt: |
          You are Alex.
    """)
    _w(d / "voice.yaml", "schema_version: 1\n")
    _w(d / "response_shape.yaml", "schema_version: 1\n")
    _w(d / "runtime.yaml", """\
        schema_version: 1
        model: sonnet
        enabled: false
        memory:
          token_budget: 0
        session:
          strategy: ephemeral
    """)
    return d


def _policies_file(base: Path) -> Path:
    p = base / "disclosure.yaml"
    _w(p, """\
        schema_version: 1
        policies:
          standard:
            categories:
              financial:
                required_trust: authenticated
                examples: [balances]
            safe_on_any_channel: [device_state]
            deflection_patterns:
              household_shared: "private."
    """)
    return p


# ---------------------------------------------------------------------------
# TestHappyPath
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_loads_resident_directory(self, tmp_path):
        from agent_loader import load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        cfg = load_agent_from_dir(str(agent_dir), policies=policies)

        assert cfg.role == "assistant"
        assert cfg.character.name == "Ellen"
        assert cfg.model == "claude-sonnet-4-6"
        assert "telegram" in cfg.channels
        # Composed prompt surfaces each section.
        assert cfg.system_prompt.startswith("You are Ellen.")
        assert "### Voice" in cfg.system_prompt
        assert "### Response shape" in cfg.system_prompt
        assert "### Disclosure" in cfg.system_prompt

    def test_loads_executor_directory(self, tmp_path):
        from agent_loader import load_agent_from_dir

        agent_dir = _seed_executor(tmp_path / "executors", "finance")

        cfg = load_agent_from_dir(str(agent_dir), policies=None)

        assert cfg.role == "finance"
        assert cfg.enabled is False
        # Executors get character + voice + response_shape only — no
        # Disclosure, no Delegation section in the prompt.
        assert cfg.system_prompt.startswith("You are Alex.")
        assert "### Disclosure" not in cfg.system_prompt
        assert "### Delegation" not in cfg.system_prompt


# ---------------------------------------------------------------------------
# TestStrictMode — unknown field / file / schema_version
# ---------------------------------------------------------------------------


class TestStrictMode:
    def test_unknown_field_in_character_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "character.yaml", """\
            schema_version: 1
            name: Ellen
            role: assistant
            archetype: assistant
            card: x
            prompt: x
            bogus_field: true
        """)
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="character.yaml"):
            load_agent_from_dir(str(agent_dir), policies=policies)

    def test_unknown_file_in_agent_dir_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "stray.yaml", "schema_version: 1\n")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="stray.yaml"):
            load_agent_from_dir(str(agent_dir), policies=policies)

    def test_dotfiles_skipped(self, tmp_path):
        """.git, .DS_Store, .swp should not trip strict-mode."""
        from agent_loader import load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / ".DS_Store", "junk")
        _w(agent_dir / ".swp.tmp", "junk")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        cfg = load_agent_from_dir(str(agent_dir), policies=policies)
        assert cfg.role == "assistant"

    def test_wrong_schema_version_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "voice.yaml", "schema_version: 2\n")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="schema_version"):
            load_agent_from_dir(str(agent_dir), policies=policies)


# ---------------------------------------------------------------------------
# TestTierRules
# ---------------------------------------------------------------------------


class TestTierRules:
    def test_resident_missing_required_file_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        (agent_dir / "disclosure.yaml").unlink()
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="disclosure.yaml"):
            load_agent_from_dir(str(agent_dir), policies=policies)

    def test_executor_with_disclosure_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError

        agent_dir = _seed_executor(tmp_path / "executors", "finance")
        _w(agent_dir / "disclosure.yaml", "schema_version: 1\npolicy: standard\n")

        with pytest.raises(LoadError, match="disclosure.yaml"):
            load_agent_from_dir(str(agent_dir), policies=None)

    def test_executor_with_triggers_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError

        agent_dir = _seed_executor(tmp_path / "executors", "finance")
        _w(agent_dir / "triggers.yaml",
           "schema_version: 1\ntriggers: []\n")

        with pytest.raises(LoadError, match="triggers.yaml"):
            load_agent_from_dir(str(agent_dir), policies=None)

    def test_role_must_match_directory(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        # character.yaml carries `role: butler` but directory is named assistant
        _w(agent_dir / "character.yaml", """\
            schema_version: 1
            name: Ellen
            role: butler
            archetype: assistant
            card: x
            prompt: x
        """)
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="role"):
            load_agent_from_dir(str(agent_dir), policies=policies)


# ---------------------------------------------------------------------------
# TestDelegatesValidation
# ---------------------------------------------------------------------------


class TestDelegatesValidation:
    def test_non_empty_delegates_require_mcp_tool(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "delegates.yaml", """\
            schema_version: 1
            delegates:
              - agent: finance
                purpose: money
                when: money q
        """)
        # runtime.yaml tools.allowed missing delegate_to_agent
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="delegate_to_agent"):
            load_agent_from_dir(str(agent_dir), policies=policies)

    def test_empty_delegates_does_not_require_mcp_tool(self, tmp_path):
        from agent_loader import load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "delegates.yaml",
           "schema_version: 1\ndelegates: []\n")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        cfg = load_agent_from_dir(str(agent_dir), policies=policies)
        assert cfg.delegates == []


# ---------------------------------------------------------------------------
# TestComposition
# ---------------------------------------------------------------------------


class TestComposition:
    def test_system_prompt_composition_order(self, tmp_path):
        """character → voice → response_shape → delegates → disclosure."""
        from agent_loader import load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "delegates.yaml", """\
            schema_version: 1
            delegates: []
        """)
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        cfg = load_agent_from_dir(str(agent_dir), policies=policies)

        prompt = cfg.system_prompt
        pos_char = prompt.index("You are Ellen.")
        pos_voice = prompt.index("### Voice")
        pos_rshape = prompt.index("### Response shape")
        pos_disc = prompt.index("### Disclosure")

        assert pos_char < pos_voice < pos_rshape < pos_disc
