"""Tests for ExecutorRegistry - Tier 3 type loader."""

from __future__ import annotations

import os
import textwrap

import pytest


def _write(base, name, enabled=True):
    d = os.path.join(base, "executors", name)
    os.makedirs(os.path.join(d, "doctrine"), exist_ok=True)
    with open(os.path.join(d, "definition.yaml"), "w") as fh:
        fh.write(textwrap.dedent(f"""\
            schema_version: 1
            type: {name}
            description: A reasonably long description that meets minLength 20.
            model: sonnet
            driver: in_casa
            enabled: {str(enabled).lower()}
            tools:
              allowed: [Read]
              permission_mode: acceptEdits
            mcp_server_names: [casa-framework]
        """))
    with open(os.path.join(d, "prompt.md"), "w") as fh:
        fh.write("Hello.")


class TestExecutorRegistry:
    def test_load_empty(self, tmp_path):
        from executor_registry import ExecutorRegistry
        r = ExecutorRegistry(str(tmp_path / "executors"))
        r.load()
        assert r.list_types() == []
        assert r.get("configurator") is None

    def test_load_one_enabled(self, tmp_path):
        from executor_registry import ExecutorRegistry
        _write(str(tmp_path), "configurator")
        r = ExecutorRegistry(str(tmp_path / "executors"))
        r.load()
        assert r.list_types() == ["configurator"]
        d = r.get("configurator")
        assert d is not None
        assert d.enabled is True

    def test_disabled_excluded_from_list(self, tmp_path):
        from executor_registry import ExecutorRegistry
        _write(str(tmp_path), "configurator", enabled=False)
        r = ExecutorRegistry(str(tmp_path / "executors"))
        r.load()
        assert r.list_types() == []
        assert r.get("configurator") is None

    def test_load_missing_dir(self, tmp_path):
        from executor_registry import ExecutorRegistry
        r = ExecutorRegistry(str(tmp_path / "nope"))
        r.load()
        assert r.list_types() == []


class TestExecutorRegistryFailedLogging:
    def test_failed_executor_logged_but_others_load(self, tmp_path, caplog):
        """B-1b regression — one broken executor must not wipe the registry."""
        import os
        import textwrap
        from executor_registry import ExecutorRegistry

        # configurator: valid
        _write(str(tmp_path), "configurator")
        # plugin-developer: broken (permission_mode typo)
        broken = textwrap.dedent("""\
            schema_version: 1
            type: plugin-developer
            description: A reasonably long description that meets minLength 20.
            model: sonnet
            driver: claude_code
            enabled: true
            tools:
              allowed: [Read]
              permission_mode: acceptedits
        """)
        d = os.path.join(str(tmp_path), "executors", "plugin-developer")
        os.makedirs(os.path.join(d, "doctrine"), exist_ok=True)
        with open(os.path.join(d, "definition.yaml"), "w") as fh:
            fh.write(broken)
        with open(os.path.join(d, "prompt.md"), "w") as fh:
            fh.write("Hello.")

        r = ExecutorRegistry(str(tmp_path / "executors"))
        with caplog.at_level("ERROR", logger="executor_registry"):
            r.load()
        # configurator loaded; plugin-developer failed but logged.
        assert r.list_types() == ["configurator"]
        assert any(
            "plugin-developer" in rec.message and "permission_mode" in rec.message
            for rec in caplog.records if rec.levelname == "ERROR"
        )
