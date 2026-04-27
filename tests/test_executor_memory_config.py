"""Unit coverage for ExecutorMemoryConfig + ExecutorDefinition.memory parsing (M4)."""

from __future__ import annotations

import json
import os

import jsonschema
import pytest

from config import ExecutorDefinition, ExecutorMemoryConfig


_SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "casa-agent", "rootfs", "opt", "casa",
    "defaults", "schema", "executor.v1.json",
)


def _load_schema() -> dict:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _base_defn() -> dict:
    return {
        "schema_version": 1,
        "type": "configurator",
        "description": "Test executor with at least twenty characters.",
        "model": "sonnet",
        "driver": "in_casa",
    }


def test_default_memory_config_disabled():
    cfg = ExecutorMemoryConfig()
    assert cfg.enabled is False
    assert cfg.token_budget == 2000


def test_executor_definition_default_memory_field():
    defn = ExecutorDefinition(
        type="x", description="x" * 20, model="sonnet", driver="in_casa",
    )
    assert isinstance(defn.memory, ExecutorMemoryConfig)
    assert defn.memory.enabled is False
    assert defn.memory.token_budget == 2000


def test_schema_accepts_definition_without_memory_block():
    """Backward compat: definitions without memory: are valid."""
    jsonschema.validate(_base_defn(), _load_schema())


def test_schema_accepts_memory_disabled():
    d = _base_defn() | {"memory": {"enabled": False}}
    jsonschema.validate(d, _load_schema())


def test_schema_accepts_memory_enabled_with_budget():
    d = _base_defn() | {"memory": {"enabled": True, "token_budget": 2000}}
    jsonschema.validate(d, _load_schema())


def test_schema_rejects_memory_missing_enabled():
    d = _base_defn() | {"memory": {"token_budget": 2000}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(d, _load_schema())


def test_schema_rejects_memory_unknown_property():
    d = _base_defn() | {"memory": {"enabled": True, "huh": 1}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(d, _load_schema())


def test_schema_rejects_token_budget_below_minimum():
    d = _base_defn() | {"memory": {"enabled": True, "token_budget": 50}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(d, _load_schema())
