"""Tests for agent_loader.load_all_executors."""

from __future__ import annotations

import os
import textwrap

import pytest


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
