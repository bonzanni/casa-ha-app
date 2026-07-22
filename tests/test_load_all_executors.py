"""Tests for agent_loader.load_all_executors."""

from __future__ import annotations

import os
import textwrap

import pytest

pytestmark = pytest.mark.asyncio


def _write_exec(base, name, defn_yaml=None, prompt="Hi."):
    d = os.path.join(base, name)
    os.makedirs(os.path.join(d, "doctrine"), exist_ok=True)
    defn = defn_yaml or textwrap.dedent(f"""\
        schema_version: 1
        type: {name}
        description: A reasonably long description that meets minLength 20.
        model: sonnet
        driver: in_casa
        enabled: true
        tools:
          allowed: [Read]
          permission_mode: acceptEdits
        mcp_server_names: [casa-framework]
    """)
    with open(os.path.join(d, "definition.yaml"), "w") as fh:
        fh.write(defn)
    with open(os.path.join(d, "prompt.md"), "w") as fh:
        fh.write(prompt)
    return d


def _seed_executor_role_artifact(roles_dir, type_name):
    """Write a minimal schema-valid canonical role artifact for an
    executor type under a test-owned roles_dir (Personality Phase A,
    Task 5 — load_all_executors now requires one, cross-validated on
    id/kind/slot, per defaults/roles/executor/<type>/). Real shipped
    types ("configurator", "plugin-developer") already have real
    artifacts under the production defaults/roles/ tree and don't need
    this; synthetic test-only types (e.g. "myx") do."""
    d = os.path.join(roles_dir, "executor", type_name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "role.yaml"), "w") as fh:
        fh.write(textwrap.dedent(f"""\
            api_version: casa.role/v1
            id: executor:{type_name}
            kind: executor
            slot: {type_name}
            mission: Test fixture executor role.
            enabled: true
            model: {{source: fixed, value: sonnet}}
            tools:
              allowed: []
              disallowed: []
              permission_mode: acceptEdits
              max_turns: 10
              skills: all
              voice_guard: none
            mcp_servers: []
            channels: []
            memory: {{token_budget: 0, read_strategy: per_turn}}
            session: {{strategy: ephemeral, idle_timeout_seconds: 0}}
            disclosure: {{policy: executor-internal, overrides: {{}}}}
            delegates: []
            executors: []
            triggers: []
            hooks: {{pre_tool_use: []}}
            tts: {{tag_dialect: none, error_phrases: {{}}}}
            response:
              text: {{register: plain}}
              voice: {{register: unavailable}}
              restricted_webhook: {{register: unavailable}}
            persona: {{policy: forbidden}}
            requires: {{plugins: [], tools: []}}
            doctrine_file: doctrine.md
        """))
    with open(os.path.join(d, "doctrine.md"), "w") as fh:
        fh.write("# Core doctrine\n\nTest fixture doctrine body.\n")
    return d


class TestLoadAllExecutors:
    def test_empty_dir_returns_empty(self, tmp_path):
        from agent_loader import load_all_executors
        os.makedirs(tmp_path / "executors", exist_ok=True)
        out, failed = load_all_executors(str(tmp_path))
        assert out == {}
        assert failed == []

    def test_happy_path_one_type(self, tmp_path):
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        _write_exec(str(base), "configurator")
        out, failed = load_all_executors(str(tmp_path))
        assert failed == []
        assert "configurator" in out
        d = out["configurator"]
        assert d.type == "configurator"
        assert d.model == "claude-sonnet-4-6"
        assert d.driver == "in_casa"
        assert d.enabled is True
        assert d.prompt_template_path.endswith("prompt.md")

    def test_schema_violation_reports_failed(self, tmp_path):
        """v0.37.1 B-1b: per-file isolation — schema violation lands
        on ``failed`` instead of raising LoadError out of the loop."""
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        bad = textwrap.dedent("""\
            schema_version: 1
            type: bad
            description: too short
            model: sonnet
            driver: in_casa
        """)
        _write_exec(str(base), "bad", defn_yaml=bad)
        out, failed = load_all_executors(str(tmp_path))
        assert out == {}
        assert len(failed) == 1
        assert failed[0][0] == "bad"

    def test_missing_prompt_reports_failed(self, tmp_path):
        """v0.37.1 B-1b: missing prompt.md is per-file failure, not raise."""
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        d = str(base / "x")
        os.makedirs(d)
        with open(os.path.join(d, "definition.yaml"), "w") as fh:
            fh.write(textwrap.dedent("""\
                schema_version: 1
                type: x
                description: Adequate description for loading tests.
                model: sonnet
                driver: in_casa
            """))
        out, failed = load_all_executors(str(tmp_path))
        assert out == {}
        assert len(failed) == 1
        assert failed[0][0] == "x"


class TestExecutorDefinitionPlan4aFields:
    def test_populates_plan4a_fields_with_defaults(self, tmp_path):
        from agent_loader import load_all_executors

        ex_dir = tmp_path / "executors" / "myx"
        ex_dir.mkdir(parents=True)
        (ex_dir / "definition.yaml").write_text(
            "schema_version: 1\n"
            "type: myx\n"
            "description: A test executor with a minimum of twenty characters.\n"
            "model: sonnet\n"
            "driver: claude_code\n"
        )
        (ex_dir / "prompt.md").write_text("hi")
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx")

        result, _failed = load_all_executors(str(tmp_path), roles_dir=str(roles_dir))
        defn = result["myx"]

        assert defn.extra_dirs == []
        assert defn.mirror_chat_to_topic is True
        assert defn.plugins_dir == ""
        assert defn.role_artifact is not None
        assert defn.role_artifact.role["id"] == "executor:myx"

    def test_reads_plan4a_fields_from_yaml(self, tmp_path):
        from agent_loader import load_all_executors

        ex_dir = tmp_path / "executors" / "myx"
        ex_dir.mkdir(parents=True)
        (ex_dir / "definition.yaml").write_text(
            "schema_version: 1\n"
            "type: myx\n"
            "description: A test executor with a minimum of twenty characters.\n"
            "model: sonnet\n"
            "driver: claude_code\n"
            "extra_dirs:\n"
            "  - /data/casa-plugins-repo\n"
            "mirror_chat_to_topic: false\n"
        )
        (ex_dir / "prompt.md").write_text("hi")
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx")

        loaded, _failed = load_all_executors(str(tmp_path), roles_dir=str(roles_dir))
        defn = loaded["myx"]

        assert defn.extra_dirs == ["/data/casa-plugins-repo"]
        assert defn.mirror_chat_to_topic is False

    def test_plugins_dir_resolves_when_plugins_subdir_exists(self, tmp_path):
        from agent_loader import load_all_executors

        ex_dir = tmp_path / "executors" / "myx"
        ex_dir.mkdir(parents=True)
        (ex_dir / "definition.yaml").write_text(
            "schema_version: 1\n"
            "type: myx\n"
            "description: A test executor with a minimum of twenty characters.\n"
            "model: sonnet\n"
            "driver: claude_code\n"
        )
        (ex_dir / "prompt.md").write_text("hi")
        (ex_dir / "plugins").mkdir()        # the positive-path trigger
        roles_dir = tmp_path / "roles"
        _seed_executor_role_artifact(str(roles_dir), "myx")

        loaded, _failed = load_all_executors(str(tmp_path), roles_dir=str(roles_dir))
        defn = loaded["myx"]

        # plugins_dir resolves to the absolute plugins/ path inside the executor dir.
        expected = str(ex_dir / "plugins")
        assert defn.plugins_dir == expected


class TestExecutorRoleArtifact:
    """Personality Phase A, Task 5: load_all_executors also loads and
    cross-validates the canonical role artifact at
    defaults/roles/executor/<type>/, isolated per-executor like every
    other failure mode."""

    def test_real_shipped_configurator_gets_role_artifact(self, tmp_path):
        """The real shipped 'configurator' type resolves against the
        production defaults/roles/ tree with no roles_dir override."""
        from agent_loader import load_all_executors

        base = tmp_path / "executors"
        _write_exec(str(base), "configurator")
        out, failed = load_all_executors(str(tmp_path))

        assert failed == []
        defn = out["configurator"]
        assert defn.role_artifact is not None
        assert defn.role_artifact.role["id"] == "executor:configurator"
        assert defn.role_artifact.role["kind"] == "executor"
        assert defn.role_artifact.role["slot"] == "configurator"
        assert defn.role_artifact.role["persona"] == {"policy": "forbidden"}

    def test_missing_role_artifact_is_isolated_failure(self, tmp_path):
        """A synthetic executor type with no matching role artifact under
        roles_dir fails closed as a per-entry failure, not a raise."""
        from agent_loader import load_all_executors

        base = tmp_path / "executors"
        _write_exec(str(base), "nope")
        empty_roles_dir = tmp_path / "empty_roles"
        empty_roles_dir.mkdir()

        out, failed = load_all_executors(str(tmp_path), roles_dir=str(empty_roles_dir))

        assert out == {}
        assert len(failed) == 1
        assert failed[0][0] == "nope"
        assert "role artifact" in failed[0][1]

    def test_mismatched_slot_is_isolated_failure(self, tmp_path):
        """A role artifact whose declared slot does not match the executor
        type directory name is rejected, not silently accepted."""
        from agent_loader import load_all_executors

        base = tmp_path / "executors"
        _write_exec(str(base), "aaa")
        roles_dir = tmp_path / "roles"
        # Seed the artifact under the WRONG slot directory name relative
        # to its own declared id/slot ("bbb" inside), so aaa/ ends up
        # holding a role.yaml that declares slot: bbb.
        _seed_executor_role_artifact(str(roles_dir), "bbb")
        import shutil
        shutil.move(
            str(roles_dir / "executor" / "bbb"),
            str(roles_dir / "executor" / "aaa"),
        )

        out, failed = load_all_executors(str(tmp_path), roles_dir=str(roles_dir))

        assert out == {}
        assert len(failed) == 1
        assert failed[0][0] == "aaa"
        assert "role artifact" in failed[0][1]

    def test_mismatched_kind_is_isolated_failure(self, tmp_path):
        """A role artifact whose declared kind is not 'executor' is
        rejected even if slot/id otherwise line up structurally."""
        from agent_loader import load_all_executors

        base = tmp_path / "executors"
        _write_exec(str(base), "ccc")
        roles_dir = tmp_path / "roles"
        d = roles_dir / "executor" / "ccc"
        d.mkdir(parents=True)
        (d / "role.yaml").write_text(textwrap.dedent("""\
            api_version: casa.role/v1
            id: resident:ccc
            kind: resident
            slot: ccc
            mission: Wrong-kind fixture.
            enabled: true
            model: {source: fixed, value: sonnet}
            tools:
              allowed: []
              disallowed: []
              permission_mode: acceptEdits
              max_turns: 10
              skills: all
              voice_guard: none
            mcp_servers: []
            channels: [telegram]
            memory: {token_budget: 0, read_strategy: per_turn}
            session: {strategy: ephemeral, idle_timeout_seconds: 0}
            disclosure: {policy: standard, overrides: {}}
            delegates: []
            executors: []
            triggers: []
            hooks: {pre_tool_use: []}
            tts: {tag_dialect: none, error_phrases: {}}
            response:
              text: {register: plain}
              voice: {register: plain}
              restricted_webhook: {register: plain}
            persona: {policy: required, compatibility: ["x@>=1.0.0 <2.0.0"]}
            requires: {plugins: [], tools: []}
            doctrine_file: doctrine.md
        """), encoding="utf-8")
        (d / "doctrine.md").write_text("# Core doctrine\n\nBody.\n", encoding="utf-8")

        out, failed = load_all_executors(str(tmp_path), roles_dir=str(roles_dir))

        assert out == {}
        assert len(failed) == 1
        assert failed[0][0] == "ccc"
        assert "role artifact" in failed[0][1]
