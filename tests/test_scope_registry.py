"""Tests for scope_registry.py — scope library + registry."""

from __future__ import annotations

import textwrap

import pytest


def _write(path, text: str) -> None:
    path.write_text(textwrap.dedent(text), encoding="utf-8")


SCOPES_YAML = """\
schema_version: 1
scopes:
  personal:
    minimum_trust: authenticated
    description: |
      Private life: relationships, friendships, non-work correspondence.
  house:
    minimum_trust: household-shared
    description: |
      The physical house: appliances, lights, plumbing, contractors.
"""


class TestScopeLibraryLoad:
    def test_loads_valid_scopes(self, tmp_path):
        from scope_registry import load_scope_library

        scopes_file = tmp_path / "scopes.yaml"
        _write(scopes_file, SCOPES_YAML)

        lib = load_scope_library(str(scopes_file))
        assert lib.names() == ["personal", "house"]
        assert lib.minimum_trust("personal") == "authenticated"
        assert lib.minimum_trust("house") == "household-shared"
        assert "Private life" in lib.description("personal")

    def test_missing_file_raises(self, tmp_path):
        from scope_registry import load_scope_library, ScopeError

        with pytest.raises(ScopeError, match="not found"):
            load_scope_library(str(tmp_path / "missing.yaml"))

    def test_wrong_schema_version_raises(self, tmp_path):
        from scope_registry import load_scope_library, ScopeError

        f = tmp_path / "scopes.yaml"
        _write(f, "schema_version: 99\nscopes: {personal: {minimum_trust: authenticated, description: '01234567890123456789'}}\n")

        with pytest.raises(ScopeError, match=r"schema violation"):
            load_scope_library(str(f))

    def test_unknown_trust_tier_raises(self, tmp_path):
        from scope_registry import load_scope_library, ScopeError

        f = tmp_path / "scopes.yaml"
        _write(f, textwrap.dedent("""\
            schema_version: 1
            scopes:
              weird:
                minimum_trust: supercalifragilistic
                description: |
                  Long enough description to pass the minLength check.
        """))

        with pytest.raises(ScopeError, match=r"schema violation"):
            load_scope_library(str(f))

    def test_description_too_short_raises(self, tmp_path):
        from scope_registry import load_scope_library, ScopeError

        f = tmp_path / "scopes.yaml"
        _write(f, textwrap.dedent("""\
            schema_version: 1
            scopes:
              x:
                minimum_trust: authenticated
                description: short
        """))

        with pytest.raises(ScopeError, match=r"schema violation"):
            load_scope_library(str(f))

    def test_unknown_name_raises_on_lookup(self, tmp_path):
        from scope_registry import load_scope_library, ScopeError

        f = tmp_path / "scopes.yaml"
        _write(f, SCOPES_YAML)
        lib = load_scope_library(str(f))

        with pytest.raises(ScopeError, match="unknown scope"):
            lib.minimum_trust("nonexistent")
