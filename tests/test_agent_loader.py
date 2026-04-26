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


def _seed_specialist(base: Path, role: str = "finance") -> Path:
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

    def test_loads_specialist_directory(self, tmp_path):
        from agent_loader import load_agent_from_dir

        agent_dir = _seed_specialist(tmp_path / "specialists", "finance")

        cfg = load_agent_from_dir(str(agent_dir), policies=None)

        assert cfg.role == "finance"
        assert cfg.enabled is False
        # Specialists get character + voice + response_shape only — no
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

    def test_specialist_with_disclosure_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError

        agent_dir = _seed_specialist(tmp_path / "specialists", "finance")
        _w(agent_dir / "disclosure.yaml", "schema_version: 1\npolicy: standard\n")

        with pytest.raises(LoadError, match="disclosure.yaml"):
            load_agent_from_dir(str(agent_dir), policies=None)

    def test_specialist_with_triggers_raises(self, tmp_path):
        from agent_loader import load_agent_from_dir, LoadError

        agent_dir = _seed_specialist(tmp_path / "specialists", "finance")
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


# ---------------------------------------------------------------------------
# TestLoadAllAgents
# ---------------------------------------------------------------------------


class TestLoadAllAgents:
    def test_finds_two_residents_skips_specialists_subdir(self, tmp_path):
        from agent_loader import load_all_agents
        from policies import load_policies

        agents_root = tmp_path / "agents"
        _seed_resident(agents_root, "assistant")
        _seed_resident(agents_root, "butler")
        _seed_specialist(agents_root / "specialists", "finance")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        found = load_all_agents(str(agents_root), policies=policies)
        assert set(found.keys()) == {"assistant", "butler"}

    def test_skips_dotdirs(self, tmp_path):
        from agent_loader import load_all_agents
        from policies import load_policies

        agents_root = tmp_path / "agents"
        _seed_resident(agents_root, "assistant")
        (agents_root / ".git").mkdir()   # git repo root sits here

        policies = load_policies(str(_policies_file(tmp_path / "policies")))
        found = load_all_agents(str(agents_root), policies=policies)
        assert set(found.keys()) == {"assistant"}

    def test_missing_dir_returns_empty(self, tmp_path):
        from agent_loader import load_all_agents
        assert load_all_agents(str(tmp_path / "nope"), policies=None) == {}


class TestLoadAllSpecialists:
    def test_finds_specialist(self, tmp_path):
        from agent_loader import load_all_specialists

        specialists_root = tmp_path / "specialists"
        _seed_specialist(specialists_root, "finance")

        found = load_all_specialists(str(specialists_root))
        assert "finance" in found


# ---------------------------------------------------------------------------
# TestBuildRoleRegistry
# ---------------------------------------------------------------------------


def test_duplicate_role_across_residents_and_specialists_fails(tmp_path):
    """A role present in BOTH residents/<role>/ and specialists/<role>/
    must fail boot — both registries cannot share a role."""
    from casa_core import _build_role_registry  # introduced by Task 5
    residents = {"finance": object()}      # placeholder configs
    specialists = {"finance": object()}    # collision on `finance`
    with pytest.raises(ValueError) as exc:
        _build_role_registry(residents=residents, specialists=specialists)
    msg = str(exc.value).lower()
    assert "duplicate" in msg
    assert "finance" in msg


def test_butler_runtime_grants_homeassistant_server_level():
    """Butler must have server-level mcp__homeassistant grant so every HA
    Assist tool the user exposes is callable without per-tool enumeration.
    Spike (2026-04-26) confirmed the SDK forwards `mcp__<server>` verbatim
    to the CC CLI, which honours it as a server-level wildcard."""
    import yaml
    from pathlib import Path

    runtime_path = (
        Path(__file__).resolve().parents[1]
        / "casa-agent/rootfs/opt/casa/defaults/agents/butler/runtime.yaml"
    )
    data = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    allowed = data["tools"]["allowed"]
    assert "mcp__homeassistant" in allowed, (
        f"butler.tools.allowed missing mcp__homeassistant; got {allowed}"
    )
