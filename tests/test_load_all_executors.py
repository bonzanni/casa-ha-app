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


class TestLoadAllExecutors:
    def test_empty_dir_returns_empty(self, tmp_path):
        from agent_loader import load_all_executors
        os.makedirs(tmp_path / "executors", exist_ok=True)
        out = load_all_executors(str(tmp_path))
        assert out == {}

    def test_happy_path_one_type(self, tmp_path):
        from agent_loader import load_all_executors
        base = tmp_path / "executors"
        _write_exec(str(base), "configurator")
        out = load_all_executors(str(tmp_path))
        assert "configurator" in out
        d = out["configurator"]
        assert d.type == "configurator"
        assert d.model == "claude-sonnet-4-6"
        assert d.driver == "in_casa"
        assert d.enabled is True
        assert d.prompt_template_path.endswith("prompt.md")

    def test_schema_violation_raises(self, tmp_path):
        from agent_loader import load_all_executors, LoadError
        base = tmp_path / "executors"
        bad = textwrap.dedent("""\
            schema_version: 1
            type: bad
            description: too short
            model: sonnet
            driver: in_casa
        """)
        _write_exec(str(base), "bad", defn_yaml=bad)
        with pytest.raises(LoadError):
            load_all_executors(str(tmp_path))

    def test_missing_prompt_raises(self, tmp_path):
        from agent_loader import load_all_executors, LoadError
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
        with pytest.raises(LoadError):
            load_all_executors(str(tmp_path))


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

        result = load_all_executors(str(tmp_path))
        defn = result["myx"]

        assert defn.extra_dirs == []
        assert defn.mirror_chat_to_topic is True
        assert defn.plugins_dir == ""

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

        defn = load_all_executors(str(tmp_path))["myx"]

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

        defn = load_all_executors(str(tmp_path))["myx"]

        # plugins_dir resolves to the absolute plugins/ path inside the executor dir.
        expected = str(ex_dir / "plugins")
        assert defn.plugins_dir == expected
