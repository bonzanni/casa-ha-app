"""Tests for executors.yaml parsing + assistant-only role validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent_loader import LoadError, _read_yaml, _validate

pytestmark = pytest.mark.asyncio


def _write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_executors_schema_accepts_minimal_valid(tmp_path):
    f = tmp_path / "executors.yaml"
    _write(f, """\
        schema_version: 1
        executors:
          - executor_type: configurator
            purpose: edit configs
            when: user wants to change configuration
    """)
    data = _read_yaml(str(f))
    _validate(data, "executors", str(f))  # must not raise


def test_executors_schema_rejects_missing_field(tmp_path):
    f = tmp_path / "executors.yaml"
    _write(f, """\
        schema_version: 1
        executors:
          - executor_type: configurator
            purpose: edit configs
            # `when` missing
    """)
    data = _read_yaml(str(f))
    with pytest.raises(LoadError) as exc:
        _validate(data, "executors", str(f))
    assert "when" in str(exc.value)


def test_executors_schema_rejects_unknown_property(tmp_path):
    f = tmp_path / "executors.yaml"
    _write(f, """\
        schema_version: 1
        executors:
          - executor_type: configurator
            purpose: edit configs
            when: x
            extra: nope
    """)
    data = _read_yaml(str(f))
    with pytest.raises(LoadError):
        _validate(data, "executors", str(f))


def test_executor_entry_dataclass_fields():
    from config import ExecutorEntry
    e = ExecutorEntry(
        executor_type="configurator",
        purpose="edit configs",
        when="user wants to change configuration",
    )
    assert e.executor_type == "configurator"
    assert e.purpose == "edit configs"
    assert e.when == "user wants to change configuration"


def test_agent_config_has_executors_field():
    from config import AgentConfig
    cfg = AgentConfig()
    assert cfg.executors == []
