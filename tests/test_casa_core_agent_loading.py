"""Tests for casa_core's agent-loading integration.

casa_core.main no longer owns the loader — it just wraps
agent_loader.load_all_agents. These tests focus on the wrapping and
directory walk behaviour, using minimal fixture trees.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _seed_resident(base: Path, role: str) -> Path:
    d = base / role
    _w(d / "character.yaml", f"""\
        schema_version: 1
        name: {role.capitalize()}
        role: {role}
        archetype: assistant
        card: |
          x
        prompt: |
          x
    """)
    _w(d / "voice.yaml", "schema_version: 1\n")
    _w(d / "response_shape.yaml", "schema_version: 1\n")
    _w(d / "disclosure.yaml", "schema_version: 1\npolicy: standard\n")
    _w(d / "runtime.yaml", """\
        schema_version: 1
        model: sonnet
        tools:
          allowed: [Read]
        channels: [telegram]
    """)
    return d


def _seed_policies(base: Path) -> Path:
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


def _seed_minimal_tree(base: Path) -> tuple[Path, Path]:
    agents_root = base / "agents"
    agents_root.mkdir()
    _seed_resident(agents_root, "assistant")
    _seed_resident(agents_root, "butler")
    policies_dir = base / "policies"
    policies_dir.mkdir()
    _seed_policies(policies_dir)
    return agents_root, policies_dir / "disclosure.yaml"


class TestLoadsWithShippedFixtures:
    def test_finds_both_residents(self, tmp_path):
        from agent_loader import load_all_agents
        from policies import load_policies

        agents_root, policy_path = _seed_minimal_tree(tmp_path)
        policy_lib = load_policies(str(policy_path))
        found = load_all_agents(str(agents_root), policies=policy_lib)
        assert set(found.keys()) == {"assistant", "butler"}

    def test_executors_subdir_skipped(self, tmp_path):
        from agent_loader import load_all_agents
        from policies import load_policies

        agents_root, policy_path = _seed_minimal_tree(tmp_path)
        # Drop an executors/ directory with a stray finance executor —
        # the Tier 1 walker must not pick it up.
        exec_dir = agents_root / "executors" / "finance"
        _w(exec_dir / "character.yaml", """\
            schema_version: 1
            name: Alex
            role: finance
            archetype: finance
            card: |
              x
            prompt: |
              x
        """)
        _w(exec_dir / "voice.yaml", "schema_version: 1\n")
        _w(exec_dir / "response_shape.yaml", "schema_version: 1\n")
        _w(exec_dir / "runtime.yaml", """\
            schema_version: 1
            model: sonnet
            enabled: false
            memory:
              token_budget: 0
            session:
              strategy: ephemeral
        """)

        policy_lib = load_policies(str(policy_path))
        found = load_all_agents(str(agents_root), policies=policy_lib)
        assert "finance" not in found
        assert set(found.keys()) == {"assistant", "butler"}

    def test_missing_directory_returns_empty(self, tmp_path):
        from agent_loader import load_all_agents
        from policies import load_policies

        policies_dir = tmp_path / "policies"
        policies_dir.mkdir()
        policy_lib = load_policies(str(_seed_policies(policies_dir)))
        found = load_all_agents(
            str(tmp_path / "nonexistent"), policies=policy_lib,
        )
        assert found == {}
