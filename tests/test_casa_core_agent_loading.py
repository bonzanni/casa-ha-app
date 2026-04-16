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
           "name: Ellen\nrole: assistant\nmodel: opus\npersonality: a\n")
    _write(str(agents / "butler.yaml"),
           "name: Tina\nrole: butler\nmodel: haiku\npersonality: b\n")
    result = _load_agents_by_role(str(agents))
    assert set(result.keys()) == {"assistant", "butler"}
    assert result["assistant"].name == "Ellen"


def test_ignores_subagents_yaml(tmp_path):
    from casa_core import _load_agents_by_role

    agents = tmp_path / "agents"
    agents.mkdir()
    _write(str(agents / "assistant.yaml"),
           "name: Ellen\nrole: assistant\nmodel: opus\npersonality: a\n")
    _write(str(agents / "subagents.yaml"),
           "automation-builder:\n  model: sonnet\n")
    result = _load_agents_by_role(str(agents))
    assert set(result.keys()) == {"assistant"}


def test_skips_unknown_role_with_log(tmp_path, caplog):
    from casa_core import _load_agents_by_role

    agents = tmp_path / "agents"
    agents.mkdir()
    _write(str(agents / "alex.yaml"),
           "name: Alex\nrole: finance\nmodel: sonnet\npersonality: c\n")
    with caplog.at_level(logging.INFO):
        result = _load_agents_by_role(str(agents))
    assert result == {}
    assert any("not in always-on set" in r.message for r in caplog.records)


def test_duplicate_role_second_skipped(tmp_path, caplog):
    from casa_core import _load_agents_by_role

    agents = tmp_path / "agents"
    agents.mkdir()
    _write(str(agents / "a1.yaml"),
           "name: A\nrole: assistant\nmodel: opus\npersonality: a\n")
    _write(str(agents / "a2.yaml"),
           "name: B\nrole: assistant\nmodel: opus\npersonality: b\n")
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
           "name: Ellen\nrole: main\nmodel: opus\npersonality: a\n")
    with caplog.at_level(logging.WARNING):
        result = _load_agents_by_role(str(agents))
    assert "assistant" in result
    assert any("deprecated" in r.message.lower() for r in caplog.records)


def test_missing_directory_returns_empty(tmp_path):
    from casa_core import _load_agents_by_role

    result = _load_agents_by_role(str(tmp_path / "nonexistent"))
    assert result == {}
