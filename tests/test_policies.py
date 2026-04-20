"""Tests for policies.py — disclosure policy resolution + rendering."""

from __future__ import annotations

import textwrap
import pytest


def _write(path, text: str) -> None:
    path.write_text(textwrap.dedent(text), encoding="utf-8")


STANDARD_POLICY = """\
schema_version: 1
policies:
  standard:
    categories:
      financial:
        required_trust: authenticated
        examples: [bank names, balances]
      medical:
        required_trust: authenticated
        examples: [conditions, medications]
    safe_on_any_channel: [device_state, sensor_state]
    deflection_patterns:
      household_shared: "I'll tell you that privately on Telegram."
      public:           "That's private — check Telegram."
"""


# ---------------------------------------------------------------------------
# TestLoad
# ---------------------------------------------------------------------------


class TestLoad:
    def test_loads_standard_policy(self, tmp_path):
        from policies import load_policies

        pol_file = tmp_path / "disclosure.yaml"
        _write(pol_file, STANDARD_POLICY)

        lib = load_policies(str(pol_file))
        assert "standard" in lib.names()

    def test_missing_file_raises(self, tmp_path):
        from policies import load_policies, PolicyError

        with pytest.raises(PolicyError, match="not found"):
            load_policies(str(tmp_path / "missing.yaml"))

    def test_wrong_schema_version_raises(self, tmp_path):
        from policies import load_policies, PolicyError

        pol_file = tmp_path / "disclosure.yaml"
        _write(pol_file, "schema_version: 99\npolicies: {}\n")

        with pytest.raises(PolicyError, match=r"schema violation.*1 was expected"):
            load_policies(str(pol_file))

    def test_unknown_field_raises(self, tmp_path):
        from policies import load_policies, PolicyError

        pol_file = tmp_path / "disclosure.yaml"
        _write(pol_file, "schema_version: 1\nbogus: 1\npolicies: {}\n")

        with pytest.raises(PolicyError, match="additional"):
            load_policies(str(pol_file))


# ---------------------------------------------------------------------------
# TestResolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_resolve_by_name(self, tmp_path):
        from policies import load_policies

        pol_file = tmp_path / "disclosure.yaml"
        _write(pol_file, STANDARD_POLICY)
        lib = load_policies(str(pol_file))

        resolved = lib.resolve("standard", overrides={})
        assert "financial" in resolved["categories"]

    def test_unknown_policy_raises(self, tmp_path):
        from policies import load_policies, PolicyError

        pol_file = tmp_path / "disclosure.yaml"
        _write(pol_file, STANDARD_POLICY)
        lib = load_policies(str(pol_file))

        with pytest.raises(PolicyError, match="unknown policy"):
            lib.resolve("bogus", overrides={})

    def test_overrides_merge_at_top_level(self, tmp_path):
        from policies import load_policies

        pol_file = tmp_path / "disclosure.yaml"
        _write(pol_file, STANDARD_POLICY)
        lib = load_policies(str(pol_file))

        resolved = lib.resolve("standard", overrides={
            "safe_on_any_channel": ["device_state"],
        })
        assert resolved["safe_on_any_channel"] == ["device_state"]
        # Unaffected categories remain.
        assert "financial" in resolved["categories"]


# ---------------------------------------------------------------------------
# TestRender
# ---------------------------------------------------------------------------


class TestRender:
    def test_render_produces_disclosure_heading(self, tmp_path):
        from policies import load_policies, render_disclosure_section

        pol_file = tmp_path / "disclosure.yaml"
        _write(pol_file, STANDARD_POLICY)
        lib = load_policies(str(pol_file))
        resolved = lib.resolve("standard", overrides={})

        rendered = render_disclosure_section(resolved)
        assert rendered.startswith("### Disclosure")
        # Every category surfaces in the rendered block.
        assert "Financial" in rendered or "financial" in rendered
        assert "household_shared" in rendered or "Telegram" in rendered
