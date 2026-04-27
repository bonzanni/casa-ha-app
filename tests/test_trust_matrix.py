"""Parametric trust × scope matrix test (spec §4.6)."""

from __future__ import annotations

import textwrap

import pytest


TRUST_MATRIX_YAML = """\
schema_version: 2
scopes:
  authed_only:
    minimum_trust: authenticated
    kind: topical
    description: |
      Scope reserved for authenticated Nicola channels.
  household:
    minimum_trust: household-shared
    kind: topical
    description: |
      Scope available in the house including voice.
  public_scope:
    minimum_trust: public
    kind: topical
    description: |
      Scope available on any channel, trust-agnostic.
"""


@pytest.fixture
def registry(tmp_path):
    from scope_registry import load_scope_library, ScopeRegistry

    f = tmp_path / "scopes.yaml"
    f.write_text(textwrap.dedent(TRUST_MATRIX_YAML), encoding="utf-8")
    return ScopeRegistry(load_scope_library(str(f)))


@pytest.mark.parametrize("channel_trust,scope,expected", [
    # internal: everything permitted
    ("internal", "authed_only", True),
    ("internal", "household", True),
    ("internal", "public_scope", True),

    # authenticated: all except... well, everything ≤ authenticated
    ("authenticated", "authed_only", True),
    ("authenticated", "household", True),
    ("authenticated", "public_scope", True),

    # external-authenticated: can't read authed_only, can read less strict
    ("external-authenticated", "authed_only", False),
    ("external-authenticated", "household", True),
    ("external-authenticated", "public_scope", True),

    # household-shared: only household + public
    ("household-shared", "authed_only", False),
    ("household-shared", "household", True),
    ("household-shared", "public_scope", True),

    # public channel: only public scope
    ("public", "authed_only", False),
    ("public", "household", False),
    ("public", "public_scope", True),
])
def test_trust_permits(registry, channel_trust, scope, expected):
    assert registry.trust_permits(scope, channel_trust) is expected
