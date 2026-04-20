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


# ---------------------------------------------------------------------------
# TestScopeRegistryTrust (non-embedding methods)
# ---------------------------------------------------------------------------


class TestTrustPermits:
    @pytest.fixture
    def lib(self, tmp_path):
        from scope_registry import load_scope_library

        f = tmp_path / "scopes.yaml"
        _write(f, SCOPES_YAML)
        return load_scope_library(str(f))

    def test_trust_ordering(self, lib):
        from scope_registry import ScopeRegistry

        reg = ScopeRegistry(lib)
        # authenticated scope on authenticated channel → permitted
        assert reg.trust_permits("personal", "authenticated") is True
        # authenticated scope on household-shared channel → denied
        assert reg.trust_permits("personal", "household-shared") is False
        # household-shared scope on authenticated channel → permitted
        assert reg.trust_permits("house", "authenticated") is True
        # household-shared scope on household-shared channel → permitted
        assert reg.trust_permits("house", "household-shared") is True
        # internal trumps everything
        assert reg.trust_permits("personal", "internal") is True
        assert reg.trust_permits("house", "internal") is True
        # public channel sees nothing that isn't minimum_trust=public
        assert reg.trust_permits("personal", "public") is False
        assert reg.trust_permits("house", "public") is False

    def test_filter_readable(self, lib):
        from scope_registry import ScopeRegistry

        reg = ScopeRegistry(lib)
        # agent readable=[personal, house], channel authenticated → both
        assert reg.filter_readable(
            ["personal", "house"], "authenticated",
        ) == ["personal", "house"]
        # same agent on household-shared → only house
        assert reg.filter_readable(
            ["personal", "house"], "household-shared",
        ) == ["house"]
        # unknown scope in agent list → silently dropped
        assert reg.filter_readable(
            ["personal", "mystery", "house"], "authenticated",
        ) == ["personal", "house"]


class TestActiveSetAndArgmax:
    @pytest.fixture
    def reg(self, tmp_path):
        from scope_registry import load_scope_library, ScopeRegistry

        f = tmp_path / "scopes.yaml"
        _write(f, SCOPES_YAML)
        return ScopeRegistry(load_scope_library(str(f)), threshold=0.35)

    def test_active_from_scores_above_threshold(self, reg):
        scores = {"personal": 0.1, "house": 0.5}
        assert reg.active_from_scores(scores, default_scope="personal") == ["house"]

    def test_active_from_scores_all_below_falls_back_to_default(self, reg):
        scores = {"personal": 0.1, "house": 0.2}
        assert reg.active_from_scores(scores, default_scope="personal") == ["personal"]

    def test_active_from_scores_empty_input_falls_back_to_default(self, reg):
        assert reg.active_from_scores({}, default_scope="personal") == ["personal"]

    def test_argmax_picks_highest(self, reg):
        scores = {"personal": 0.4, "house": 0.8}
        assert reg.argmax_scope(scores, default_scope="personal") == "house"

    def test_argmax_falls_back_when_all_below_threshold(self, reg):
        scores = {"personal": 0.1, "house": 0.2}
        assert reg.argmax_scope(scores, default_scope="personal") == "personal"

    def test_argmax_empty_input_returns_default(self, reg):
        assert reg.argmax_scope({}, default_scope="house") == "house"
