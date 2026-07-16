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


def test_runtime_loads_context_surface_policy(tmp_path):
    from agent_loader import load_agent_from_dir
    from policies import load_policies

    agent_dir = _seed_resident(tmp_path / "agents", "butler")
    _w(agent_dir / "runtime.yaml", """\
        schema_version: 1
        model: sonnet
        tools:
          allowed: [mcp__casa-framework__get_schedule]
          skills: none
          voice_guard: ha_direct
        channels: [ha_voice]
    """)
    policies = load_policies(str(_policies_file(tmp_path / "policies")))

    cfg = load_agent_from_dir(str(agent_dir), policies=policies)

    assert cfg.tools.skills == "none"
    assert cfg.tools.voice_guard == "ha_direct"


def test_runtime_context_policy_defaults_preserve_existing_agents(tmp_path):
    from agent_loader import load_agent_from_dir
    from policies import load_policies

    agent_dir = _seed_resident(tmp_path / "agents", "assistant")
    policies = load_policies(str(_policies_file(tmp_path / "policies")))

    cfg = load_agent_from_dir(str(agent_dir), policies=policies)

    assert cfg.tools.skills == "all"
    assert cfg.tools.voice_guard == "none"


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

    @pytest.mark.parametrize(
        "backup_name",
        [
            "character.yaml.bak",
            "runtime.yaml.swp",
            "voice.yaml.tmp",
            "response_shape.yaml.orig",
            "runtime.yaml~",
        ],
    )
    def test_editor_backups_skipped(self, tmp_path, backup_name):
        """S-1 regression: sed -i.bak / vim .swp / *~ MUST NOT trip
        strict-mode. The agent on disk is still valid; the editor
        artifact is process state, not configuration."""
        from agent_loader import load_agent_from_dir
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / backup_name, "ignored editor artifact")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        cfg = load_agent_from_dir(str(agent_dir), policies=policies)
        assert cfg.role == "assistant"

    def test_genuine_unknown_file_still_raises(self, tmp_path):
        """S-1 negative: real unknown files (no backup suffix) MUST
        still raise. The whitelist must not be a wildcard."""
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "extra.yaml", "schema_version: 1\n")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError, match="extra.yaml"):
            load_agent_from_dir(str(agent_dir), policies=policies)

    def test_unknown_file_diagnostic_mentions_recovery(self, tmp_path):
        """S-1 UX: diagnostic message MUST point operators at the
        recovery path (mention editor-backup whitelist + git restore)."""
        from agent_loader import load_agent_from_dir, LoadError
        from policies import load_policies

        agent_dir = _seed_resident(tmp_path / "agents", "assistant")
        _w(agent_dir / "extra.yaml", "schema_version: 1\n")
        policies = load_policies(str(_policies_file(tmp_path / "policies")))

        with pytest.raises(LoadError) as exc_info:
            load_agent_from_dir(str(agent_dir), policies=policies)

        msg = str(exc_info.value)
        assert ".bak" in msg, f"diagnostic should mention editor backups: {msg!r}"
        assert "git" in msg.lower(), f"diagnostic should mention git restore: {msg!r}"

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

        found, failed = load_all_specialists(str(specialists_root))
        assert "finance" in found
        assert failed == []

    def test_per_specialist_isolation(self, tmp_path):
        """O-2b (v0.37.9): one malformed specialist does NOT prevent its
        siblings from loading; the bad one is recorded in ``failed``.
        Mirrors the v0.37.1 B-1b ``load_all_executors`` pattern."""
        from agent_loader import load_all_specialists

        specialists_root = tmp_path / "specialists"
        _seed_specialist(specialists_root, "finance")
        # Sibling directory with no required files → load fails.
        (specialists_root / "broken").mkdir()

        found, failed = load_all_specialists(str(specialists_root))
        assert "finance" in found
        assert any(name == "broken" for name, _ in failed)
        # Error message must name the missing required file so the
        # operator can see what went wrong.
        broken_msg = next(err for name, err in failed if name == "broken")
        assert "runtime.yaml" in broken_msg


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


def test_butler_runtime_sets_minimal_context_surface_policy():
    import yaml
    from pathlib import Path

    runtime_path = (
        Path(__file__).resolve().parents[1]
        / "casa-agent/rootfs/opt/casa/defaults/agents/butler/runtime.yaml"
    )
    data = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))

    assert data["tools"]["skills"] == "none"
    assert data["tools"]["voice_guard"] == "ha_direct"
    assert "Skill" not in data["tools"]["allowed"]
    assert data["mcp_server_names"] == ["homeassistant", "casa-framework"]


def test_assistant_runtime_omits_unused_homeassistant_attachment():
    import yaml
    from pathlib import Path

    runtime_path = (
        Path(__file__).resolve().parents[1]
        / "casa-agent/rootfs/opt/casa/defaults/agents/assistant/runtime.yaml"
    )
    data = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))

    assert "mcp__homeassistant" not in data["tools"]["allowed"]
    assert "homeassistant" not in data["mcp_server_names"]


def test_assistant_delegates_include_butler():
    """Ellen's delegates.yaml must list butler so the v0.15.0 <delegates>
    block advertises Tina to her and delegate_to_agent('butler', ...) passes
    the role-map gate."""
    import yaml
    from pathlib import Path

    delegates_path = (
        Path(__file__).resolve().parents[1]
        / "casa-agent/rootfs/opt/casa/defaults/agents/assistant/delegates.yaml"
    )
    data = yaml.safe_load(delegates_path.read_text(encoding="utf-8"))
    delegate_roles = [d["agent"] for d in data["delegates"]]
    assert "butler" in delegate_roles, (
        f"assistant.delegates missing butler; got {delegate_roles}"
    )


def test_runtime_yaml_loads_cross_peer_token_budget(tmp_path):
    """M6 § 6.3: residents may declare memory.cross_peer_token_budget
    in runtime.yaml; agent_loader populates MemoryConfig with it.

    Default value is 2000 when omitted (spec § 6.3)."""
    from agent_loader import load_agent_from_dir
    from policies import load_policies

    policies = load_policies(str(_policies_file(tmp_path / "policies")))

    # Test 1: explicit value round-trips through the loader.
    explicit_dir = _seed_resident(tmp_path / "agents_explicit", "assistant")
    _w(explicit_dir / "runtime.yaml", """\
        schema_version: 1
        model: sonnet
        tools:
          allowed: [Read, Write]
        memory:
          cross_peer_token_budget: 4000
        channels: [telegram]
    """)
    cfg_explicit = load_agent_from_dir(str(explicit_dir), policies=policies)
    assert cfg_explicit.memory.cross_peer_token_budget == 4000

    # Test 2: default value (2000) when the key is omitted.
    default_dir = _seed_resident(tmp_path / "agents_default", "assistant")
    cfg_default = load_agent_from_dir(str(default_dir), policies=policies)
    assert cfg_default.memory.cross_peer_token_budget == 2000


# ---------------------------------------------------------------------------
# TestValidateConfigRepo (E-G v0.31.0 pre-commit gate)
# ---------------------------------------------------------------------------


class TestValidateConfigRepo:
    """Pre-commit schema-validation gate for ``config_git_commit``. Repros
    the v0.30.0 P4.2 finding: configurator wrote ``TRAIT:`` as a top-level
    YAML key in character.yaml; ``additionalProperties: False`` rejects
    it on next boot. The gate refuses such commits before they land."""

    def test_clean_repo_returns_empty(self, tmp_path):
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa-agent"
        _seed_resident(repo / "agents", "assistant")
        _seed_specialist(repo / "agents", "finance")
        # A genuinely-bootable repo has a policy library (boot's load_policies
        # is unguarded); without it the add-on crash-loops (M5).
        _policies_file(repo / "policies")

        errors = validate_config_repo(str(repo))
        assert errors == []

    def test_no_agents_dir_returns_empty(self, tmp_path):
        """Fresh repo without agents/ subtree (e.g., immediately after init
        but before seed-copy) returns an empty error list rather than
        raising. Defensive: validate_config_repo is called from a
        privileged tool path and must not crash on edge filesystems."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa-agent"
        repo.mkdir(parents=True)
        errors = validate_config_repo(str(repo))
        assert errors == []

    def test_valid_policies_disclosure_passes(self, tmp_path):
        """v0.37.12 — policies/disclosure.yaml is now actively validated
        (against policy-disclosure.v1, NOT the agent disclosure schema).
        A valid file must pass cleanly. Historical note: v0.31.0 walked
        the whole repo with a flat basename map and mis-applied the
        agent schema here; v0.31.1 scoped to agents/ only as a stopgap;
        v0.37.12 introduces a path-aware policies/ walk so the
        configurator can't ship schema-invalid policy YAML."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa-agent"
        _seed_resident(repo / "agents", "assistant")
        _w(repo / "policies" / "disclosure.yaml", """\
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

        errors = validate_config_repo(str(repo))
        assert errors == [], (
            f"valid policies/disclosure.yaml must pass; got {errors}"
        )

    def test_invalid_policies_disclosure_caught(self, tmp_path):
        """policies/disclosure.yaml with a top-level unknown key fails
        ``additionalProperties: False`` on policy-disclosure.v1 and is
        surfaced by the gate. Repro of the E-G class of bug applied to
        policies/."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa-agent"
        _seed_resident(repo / "agents", "assistant")
        _w(repo / "policies" / "disclosure.yaml", """\
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
            BOGUS_POLICY_KEY: surprise
        """)

        errors = validate_config_repo(str(repo))
        assert len(errors) == 1
        assert "disclosure.yaml" in errors[0]
        assert "BOGUS_POLICY_KEY" in errors[0]
        assert "schema violation" in errors[0]

    def test_top_level_unknown_key_in_character_caught(self, tmp_path):
        """Exact repro of the v0.30.0 / v0.29.0 P4.2 'TRAIT:' incident."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa-agent"
        agent_dir = _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")  # isolate the character.yaml error

        # Append the offending top-level key, mirroring the configurator's
        # actual diff (commit 5cd731ac on 2026-04-30).
        _w(agent_dir / "character.yaml", """\
            schema_version: 1
            name: Ellen
            role: assistant
            archetype: assistant
            card: |
              Peer summary.
            prompt: |
              You are Ellen.
            TRAIT: greets warmly but keeps replies efficient.
        """)

        errors = validate_config_repo(str(repo))
        assert len(errors) == 1
        assert "character.yaml" in errors[0]
        assert "TRAIT" in errors[0]
        assert "schema violation" in errors[0]

    def test_skips_non_schema_files(self, tmp_path):
        """Markdown doctrine, plugin sources, READMEs etc. must NOT trip
        the gate — they're outside the schema-bearing YAML allowlist."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa-agent"
        _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        _w(repo / "agents" / "assistant" / "prompts" / "system.md",
           "Some prompt body — not schema-validated.\n")
        _w(repo / "doctrine.md", "free-form notes\n")
        # README inside policies/ is not a schema-bearing file.
        _w(repo / "policies" / "README.md", "free-form policy notes\n")

        errors = validate_config_repo(str(repo))
        assert errors == []

    def test_skips_dotgit_dir(self, tmp_path):
        """``.git/`` inside agents/ must NOT be walked — paranoia (a real
        repo's .git would be at the config_dir root, but a buggy nested
        copy could land schema-named blobs inside agents/)."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa-agent"
        _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")
        # Write a file inside agents/.git that would FAIL validation if visited.
        _w(repo / "agents" / ".git" / "objects" / "character.yaml",
           "TRAIT: garbage\n")

        errors = validate_config_repo(str(repo))
        assert errors == []

    def test_aggregates_multiple_offenders(self, tmp_path):
        """Multiple bad files all surface — caller sees the full picture."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "addon_configs" / "casa-agent"
        agent_dir = _seed_resident(repo / "agents", "assistant")
        _policies_file(repo / "policies")  # isolate the two character.yaml errors
        _w(agent_dir / "character.yaml", """\
            schema_version: 1
            name: Ellen
            role: assistant
            archetype: assistant
            card: x
            prompt: x
            BOGUS_A: 1
        """)
        spec_dir = _seed_specialist(repo / "agents", "finance")
        _w(spec_dir / "character.yaml", """\
            schema_version: 1
            name: Alex
            role: finance
            archetype: finance
            card: x
            prompt: x
            BOGUS_B: 2
        """)

        errors = validate_config_repo(str(repo))
        assert len(errors) == 2
        joined = "\n".join(errors)
        assert "BOGUS_A" in joined
        assert "BOGUS_B" in joined


@pytest.mark.unit
class TestValidateConfigRepoBootParity:
    """M5: the gate must refuse anything load_all_agents would FATAL on at
    boot — cross-file invariants that pass per-file schema validation yet
    crash-loop the add-on on the next boot."""

    def _policies(self, repo: Path) -> None:
        _w(repo / "policies" / "disclosure.yaml", """\
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

    def test_copied_resident_with_stale_role_refused(self, tmp_path):
        import shutil
        from agent_loader import validate_config_repo, load_agent_from_dir, LoadError

        repo = tmp_path / "cfg"
        src = _seed_resident(repo / "agents", "assistant")
        self._policies(repo)
        # Copy the whole dir but leave character.yaml role: assistant.
        shutil.copytree(src, repo / "agents" / "helper")

        with pytest.raises(LoadError):  # boot fatals on this dir
            load_agent_from_dir(str(repo / "agents" / "helper"), policies=None)

        errors = validate_config_repo(str(repo))
        assert any("must match directory name" in e for e in errors), errors

    def test_stray_unknown_file_in_resident_refused(self, tmp_path):
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        d = _seed_resident(repo / "agents", "assistant")
        self._policies(repo)
        _w(d / "notes.yaml", "anything: true\n")  # unmapped basename: schema walk skips it

        errors = validate_config_repo(str(repo))
        assert any("unknown file" in e for e in errors), errors

    def test_schema_valid_executors_yaml_on_non_assistant_refused(self, tmp_path):
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        d = _seed_resident(repo / "agents", "butler")
        self._policies(repo)
        _w(d / "executors.yaml", """\
            schema_version: 1
            executors:
              - executor_type: configurator
                purpose: config edits
                when: on request
        """)

        errors = validate_config_repo(str(repo))
        assert any("only allowed on the assistant role" in e for e in errors), errors

    def test_delegates_without_delegate_tool_refused(self, tmp_path):
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        d = _seed_resident(repo / "agents", "assistant")  # tools.allowed = [Read, Write]
        self._policies(repo)
        _w(d / "delegates.yaml", """\
            schema_version: 1
            delegates:
              - {agent: finance, purpose: money, when: asked}
        """)

        errors = validate_config_repo(str(repo))
        assert any("delegate_to_agent" in e for e in errors), errors

    def test_stray_non_directory_at_agents_root_refused(self, tmp_path):
        """M5 gate-bypass: a schema-valid (or non-schema) stray FILE directly
        under agents/ passes the per-file schema walk but crash-loops boot —
        load_all_agents RAISES LoadError on any non-directory at agents/ root.
        The parity loop must report it, not silently skip it."""
        from agent_loader import validate_config_repo, load_all_agents, LoadError

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        self._policies(repo)
        # Stray non-schema file at agents/ root: schema walk ignores it,
        # but boot fatals on it.
        _w(repo / "agents" / "scratch.md", "stray note, not an agent\n")

        with pytest.raises(LoadError):  # boot fatals on this entry
            load_all_agents(str(repo / "agents"), policies=None)

        errors = validate_config_repo(str(repo))
        assert any("non-directory" in e and "scratch.md" in e for e in errors), errors

    def test_missing_policies_file_with_resident_refused(self, tmp_path):
        """M5 gate-bypass: a resident with a disclosure.yaml but NO
        policies/disclosure.yaml passes every per-file schema check, yet
        boot's ``load_policies`` (casa_core.main line 1245, unguarded)
        RAISES ``PolicyError`` and crash-loops the add-on. The gate must
        report the missing policy library, not suppress the resulting
        'no PolicyLibrary' compose cascade."""
        from agent_loader import validate_config_repo
        from policies import PolicyError, load_policies

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")  # has disclosure.yaml
        # Deliberately NO policies/disclosure.yaml — the bypass under test.

        # Prove boot fatals exactly as casa_core.main would at line 1245.
        with pytest.raises(PolicyError):
            load_policies(str(repo / "policies" / "disclosure.yaml"))

        errors = validate_config_repo(str(repo))
        assert any("disclosure.yaml" in e and "not found" in e for e in errors), errors
        # The single missing-policies error is authoritative; the per-resident
        # 'no PolicyLibrary' cascade rooted in that same absence must NOT
        # double-report.
        assert not any("no PolicyLibrary" in e for e in errors), errors

    def test_missing_policies_file_no_agents_dir_passes(self, tmp_path):
        """A fresh repo with no agents/ subtree (pre-seed) does not trip the
        missing-policies check — boot never reaches load_all_agents there,
        matching the existing no-agents-dir contract."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        repo.mkdir(parents=True)
        errors = validate_config_repo(str(repo))
        assert errors == [], errors

    def test_clean_repo_with_policies_passes(self, tmp_path):
        """A schema- and boot-valid repo still returns no errors."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")
        self._policies(repo)

        errors = validate_config_repo(str(repo))
        assert errors == [], errors

    def _enable_specialist(self, spec_dir: Path, role: str) -> None:
        """Rewrite a seeded specialist's runtime.yaml to enabled: true so it
        reaches SpecialistRegistry.all_configs() at boot (the seed default is
        enabled: false, which would be dropped and never collide)."""
        _w(spec_dir / "runtime.yaml", f"""\
            schema_version: 1
            model: sonnet
            enabled: true
            memory:
              token_budget: 0
            session:
              strategy: ephemeral
        """)

    def test_resident_specialist_role_collision_refused(self, tmp_path):
        """M5 gate-bypass: a repo with agents/<r>/ (valid resident) AND an
        ENABLED agents/specialists/<r>/ (valid specialist) passes every
        per-file schema check and the per-agent load, yet crash-loops boot.

        casa_core.main (line ~1280) calls, UNGUARDED,
            _build_role_registry(residents=role_configs,
                                  specialists=specialist_registry.all_configs())
        which RAISES ValueError 'duplicate role(s) across residents and
        specialists' when a role exists in both tiers. config_sync does not
        heal it. The default image ships agents/specialists/finance/, so a
        configurator that creates a finance resident trips this. The gate
        must refuse the collision by replaying that registry build."""
        from agent_loader import (
            validate_config_repo,
            load_all_agents,
            load_all_specialists,
        )
        from policies import load_policies
        from casa_core import _build_role_registry

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "finance")            # resident finance
        spec = _seed_specialist(repo / "agents" / "specialists", "finance")
        self._enable_specialist(spec, "finance")              # enabled specialist finance
        self._policies(repo)

        # Prove boot fatals exactly as casa_core.main would at line ~1280:
        # residents + enabled specialists both carry role 'finance'.
        policy_lib = load_policies(str(repo / "policies" / "disclosure.yaml"))
        residents = load_all_agents(str(repo / "agents"), policies=policy_lib)
        spec_found, _ = load_all_specialists(str(repo / "agents" / "specialists"))
        with pytest.raises(ValueError):
            _build_role_registry(residents=residents, specialists=spec_found)

        errors = validate_config_repo(str(repo))
        assert any(
            "duplicate role" in e and "finance" in e for e in errors
        ), errors

    def test_disabled_specialist_role_collision_passes(self, tmp_path):
        """Boot parity, negative direction: a DISABLED specialist sharing a
        resident's role does NOT collide at boot — SpecialistRegistry.load
        drops disabled specialists before all_configs(), so
        _build_role_registry never sees the overlap. The gate must mirror
        that enablement filter and NOT over-refuse the (bootable) repo.

        The repo carries a primary assistant resident too, so the
        no-primary-assistant invariant (below) does not fire — this test
        isolates the collision path, not the missing-assistant path."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "assistant")          # primary assistant
        _seed_resident(repo / "agents", "finance")            # resident finance
        # Seeded specialist defaults to enabled: false — dropped at boot.
        _seed_specialist(repo / "agents" / "specialists", "finance")
        self._policies(repo)

        errors = validate_config_repo(str(repo))
        assert errors == [], errors

    def test_no_primary_assistant_butler_only_refused(self, tmp_path):
        """M5 gate-bypass: a committed tree whose only resident is a valid
        NON-assistant (e.g. butler) passes every per-file schema check and the
        per-agent load, yet crash-loops boot. casa_core.main (line ~1306)
        RAISES RuntimeError 'No agent with role \\'assistant\\' found ... Casa
        cannot start without a primary assistant' because role_configs holds no
        'assistant'. The gate must refuse it."""
        from agent_loader import validate_config_repo, load_all_agents
        from policies import load_policies

        repo = tmp_path / "cfg"
        _seed_resident(repo / "agents", "butler")   # enabled, non-assistant
        self._policies(repo)

        # Prove boot reaches an assistant-less role_configs: load_all_agents
        # succeeds (butler is valid) but yields no 'assistant' key, which
        # casa_core.main then FATALs on.
        policy_lib = load_policies(str(repo / "policies" / "disclosure.yaml"))
        residents = load_all_agents(str(repo / "agents"), policies=policy_lib)
        assert "assistant" not in residents

        errors = validate_config_repo(str(repo))
        assert any("primary assistant" in e for e in errors), errors

    def test_no_primary_assistant_empty_agents_dir_refused(self, tmp_path):
        """M5 gate-bypass: an existing but EMPTY agents/ dir (no resident
        subdirs) passes the per-file walk trivially, yet boot's load_all_agents
        returns {} and casa_core.main FATALs on the missing primary assistant.
        The gate must refuse it."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        (repo / "agents").mkdir(parents=True)   # exists, but empty
        self._policies(repo)

        errors = validate_config_repo(str(repo))
        assert any("primary assistant" in e for e in errors), errors

    def test_no_primary_assistant_only_disabled_specialist_refused(self, tmp_path):
        """M5 gate-bypass: the only 'assistant-ish' entry is a DISABLED
        specialist (agents/specialists/assistant/, enabled: false). It is
        dropped before the role registry, so boot has no resident 'assistant'
        and FATALs. The gate must refuse it."""
        from agent_loader import validate_config_repo

        repo = tmp_path / "cfg"
        # No residents at all; only a disabled specialist carrying role assistant.
        _seed_specialist(repo / "agents" / "specialists", "assistant")
        self._policies(repo)

        errors = validate_config_repo(str(repo))
        assert any("primary assistant" in e for e in errors), errors

    def test_resident_unknown_model_reported_not_crashed(self, tmp_path):
        """M5 defensive contract: a resident runtime.yaml naming an unknown
        model shortname is 100% schema-valid (``model`` is a free string), but
        boot's ``resolve_model`` RAISES ValueError inside load_agent_from_dir
        — a NON-LoadError. The per-resident replay must REPORT it as a
        validation error, never let it escape and crash the gate itself (the
        mandate: 'a validation error must be reported, never itself crash the
        gate')."""
        from agent_loader import validate_config_repo, load_all_agents
        from policies import load_policies

        repo = tmp_path / "cfg"
        d = _seed_resident(repo / "agents", "assistant")
        self._policies(repo)
        _w(d / "runtime.yaml", """\
            schema_version: 1
            model: bogusmodel
            tools:
              allowed: [Read, Write]
            channels: [telegram]
        """)

        # Prove boot fatals with a NON-LoadError exactly as casa_core.main
        # would: load_all_agents raises ValueError, not LoadError.
        policy_lib = load_policies(str(repo / "policies" / "disclosure.yaml"))
        with pytest.raises(ValueError):
            load_all_agents(str(repo / "agents"), policies=policy_lib)

        # The gate must RETURN an error, not raise.
        errors = validate_config_repo(str(repo))
        assert any(
            "bogusmodel" in e or "model shortname" in e for e in errors
        ), errors

    def test_resident_unknown_policy_reported_not_crashed(self, tmp_path):
        """M5 defensive contract: a resident disclosure.yaml naming a policy
        absent from policies/disclosure.yaml is 100% schema-valid (``policy``
        is a free string), but boot's _compose_prompt -> policies.resolve
        RAISES PolicyError inside load_agent_from_dir — a NON-LoadError. The
        per-resident replay must REPORT it, never crash the gate."""
        from agent_loader import validate_config_repo, load_all_agents
        from policies import PolicyError, load_policies

        repo = tmp_path / "cfg"
        d = _seed_resident(repo / "agents", "assistant")
        self._policies(repo)  # defines only policy 'standard'
        _w(d / "disclosure.yaml", """\
            schema_version: 1
            policy: nonexistent
        """)

        # Prove boot fatals with a NON-LoadError: load_all_agents (which
        # composes the prompt) raises PolicyError, not LoadError.
        policy_lib = load_policies(str(repo / "policies" / "disclosure.yaml"))
        with pytest.raises(PolicyError):
            load_all_agents(str(repo / "agents"), policies=policy_lib)

        errors = validate_config_repo(str(repo))
        assert any(
            "nonexistent" in e or "unknown policy" in e for e in errors
        ), errors
