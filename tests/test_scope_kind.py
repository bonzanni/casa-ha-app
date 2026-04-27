"""Unit coverage for scopes.yaml v2 schema + ScopeLibrary/Registry kind() accessor."""

from __future__ import annotations

import textwrap

import pytest

from scope_registry import ScopeError, ScopeLibrary, ScopeRegistry, load_scope_library


def _write_scopes(tmp_path, body: str):
    p = tmp_path / "scopes.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


def test_v2_loader_accepts_topical_with_description(tmp_path):
    path = _write_scopes(tmp_path, """
        schema_version: 2
        scopes:
          personal:
            minimum_trust: authenticated
            kind: topical
            description: |
              private life, friendships, family, weekend plans, hobbies
    """)
    lib = load_scope_library(path)
    assert lib.kind("personal") == "topical"
    assert "private life" in lib.description("personal")


def test_v2_loader_accepts_system_without_description(tmp_path):
    path = _write_scopes(tmp_path, """
        schema_version: 2
        scopes:
          meta:
            minimum_trust: authenticated
            kind: system
    """)
    lib = load_scope_library(path)
    assert lib.kind("meta") == "system"


def test_v2_loader_rejects_topical_missing_description(tmp_path):
    path = _write_scopes(tmp_path, """
        schema_version: 2
        scopes:
          personal:
            minimum_trust: authenticated
            kind: topical
    """)
    with pytest.raises(ScopeError):
        load_scope_library(path)


def test_v2_loader_rejects_system_with_description(tmp_path):
    path = _write_scopes(tmp_path, """
        schema_version: 2
        scopes:
          meta:
            minimum_trust: authenticated
            kind: system
            description: |
              system scopes are not embedded; descriptions are forbidden here
    """)
    with pytest.raises(ScopeError):
        load_scope_library(path)


def test_v2_loader_rejects_v1_schema(tmp_path):
    path = _write_scopes(tmp_path, """
        schema_version: 1
        scopes:
          personal:
            minimum_trust: authenticated
            description: |
              private life, friendships
    """)
    with pytest.raises(ScopeError):
        load_scope_library(path)


def test_registry_kind_delegates_to_library(tmp_path):
    path = _write_scopes(tmp_path, """
        schema_version: 2
        scopes:
          meta:
            minimum_trust: authenticated
            kind: system
          personal:
            minimum_trust: authenticated
            kind: topical
            description: |
              private life, friendships, weekend plans
    """)
    lib = load_scope_library(path)
    reg = ScopeRegistry(lib)
    assert reg.kind("meta") == "system"
    assert reg.kind("personal") == "topical"


def test_registry_kind_unknown_scope_raises(tmp_path):
    path = _write_scopes(tmp_path, """
        schema_version: 2
        scopes:
          personal:
            minimum_trust: authenticated
            kind: topical
            description: |
              private life, friendships
    """)
    lib = load_scope_library(path)
    reg = ScopeRegistry(lib)
    with pytest.raises(ScopeError):
        reg.kind("nope")
