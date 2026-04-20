"""Tests for casa_core._load_agents_by_role."""

import logging
import os

import pytest


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_loads_assistant_and_butler(tmp_path):
    from casa_core import _load_agents_by_role

    agents = tmp_path / "agents"
    agents.mkdir()
    _write(str(agents / "assistant.yaml"),
           "name: Ellen\nrole: assistant\nmodel: opus\npersonality: a\n"
           "channels: [telegram]\n")
    _write(str(agents / "butler.yaml"),
           "name: Tina\nrole: butler\nmodel: haiku\npersonality: b\n"
           "channels: [ha_voice]\n")
    result = _load_agents_by_role(str(agents))
    assert set(result.keys()) == {"assistant", "butler"}
    assert result["assistant"].name == "Ellen"


def test_ignores_subagents_yaml(tmp_path):
    from casa_core import _load_agents_by_role

    agents = tmp_path / "agents"
    agents.mkdir()
    _write(str(agents / "assistant.yaml"),
           "name: Ellen\nrole: assistant\nmodel: opus\npersonality: a\n"
           "channels: [telegram]\n")
    _write(str(agents / "subagents.yaml"),
           "automation-builder:\n  model: sonnet\n")
    result = _load_agents_by_role(str(agents))
    assert set(result.keys()) == {"assistant"}


def test_duplicate_role_second_skipped(tmp_path, caplog):
    from casa_core import _load_agents_by_role

    agents = tmp_path / "agents"
    agents.mkdir()
    _write(str(agents / "a1.yaml"),
           "name: A\nrole: assistant\nmodel: opus\npersonality: a\n"
           "channels: [telegram]\n")
    _write(str(agents / "a2.yaml"),
           "name: B\nrole: assistant\nmodel: opus\npersonality: b\n"
           "channels: [telegram]\n")
    with caplog.at_level(logging.ERROR):
        result = _load_agents_by_role(str(agents))
    assert result["assistant"].name == "A"  # lexicographic order: a1 first
    assert any("Duplicate role" in r.message for r in caplog.records)


def test_legacy_main_file_migrates_via_config(tmp_path, caplog):
    """Sanity check: a file with `role: main` still ends up as assistant
    (normalized by config._normalize_role)."""
    from casa_core import _load_agents_by_role

    agents = tmp_path / "agents"
    agents.mkdir()
    _write(str(agents / "assistant.yaml"),
           "name: Ellen\nrole: main\nmodel: opus\npersonality: a\n"
           "channels: [telegram]\n")
    with caplog.at_level(logging.WARNING):
        result = _load_agents_by_role(str(agents))
    assert "assistant" in result
    assert any("deprecated" in r.message.lower() for r in caplog.records)


def test_missing_directory_returns_empty(tmp_path):
    from casa_core import _load_agents_by_role

    result = _load_agents_by_role(str(tmp_path / "nonexistent"))
    assert result == {}


# ---------------------------------------------------------------------------
# Phase 3.1 — tier-boundary loader (channels: required)
# ---------------------------------------------------------------------------


def test_loads_any_role_with_channels(tmp_path):
    """A new resident YAML with non-empty `channels:` is loaded
    regardless of role name — allowlist is gone."""
    from casa_core import _load_agents_by_role

    agents = tmp_path / "agents"
    agents.mkdir()
    _write(str(agents / "guest.yaml"),
           "name: Guest\nrole: guest\nmodel: haiku\npersonality: g\n"
           "channels: [telegram]\n")
    result = _load_agents_by_role(str(agents))
    assert "guest" in result
    assert result["guest"].channels == ["telegram"]


def test_skips_when_channels_empty(tmp_path, caplog):
    """An agent YAML with missing / empty `channels:` is NOT loaded
    as a resident (Tier 2 shape — must live in agents/executors/)."""
    from casa_core import _load_agents_by_role

    agents = tmp_path / "agents"
    agents.mkdir()
    _write(str(agents / "stateless.yaml"),
           "name: S\nrole: stateless\nmodel: haiku\npersonality: s\n")
    with caplog.at_level(logging.ERROR):
        result = _load_agents_by_role(str(agents))
    assert "stateless" not in result
    assert any(
        "requires non-empty 'channels:'" in r.message
        for r in caplog.records
    )


def test_executors_subdir_not_scanned(tmp_path):
    """agents/executors/*.yaml must NOT be picked up by the Tier 1 loader.
    os.listdir is non-recursive — files in a subdirectory are invisible."""
    from casa_core import _load_agents_by_role

    agents = tmp_path / "agents"
    agents.mkdir()
    executors = agents / "executors"
    executors.mkdir()
    # Tier 2 shape: no channels.
    _write(str(executors / "alex.yaml"),
           "name: Alex\nrole: alex\nmodel: sonnet\npersonality: a\n")
    # Tier 1 resident for comparison.
    _write(str(agents / "assistant.yaml"),
           "name: E\nrole: assistant\nmodel: opus\npersonality: e\n"
           "channels: [telegram]\n")
    result = _load_agents_by_role(str(agents))
    assert set(result.keys()) == {"assistant"}
