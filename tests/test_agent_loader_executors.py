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


# ---------------------------------------------------------------------------
# Task 3: loader wiring + assistant-only validation
# ---------------------------------------------------------------------------


def test_executors_yaml_parses_on_assistant(tmp_path):
    """A valid agents/<assistant>/executors.yaml loads into cfg.executors."""
    from agent_loader import load_agent_from_dir
    from policies import load_policies
    try:
        from tests.test_agent_loader import _seed_resident, _policies_file
    except ImportError:
        from test_agent_loader import _seed_resident, _policies_file

    role = "assistant"
    d = _seed_resident(tmp_path / "agents", role=role)
    _write(d / "executors.yaml", """\
        schema_version: 1
        executors:
          - executor_type: configurator
            purpose: edit configs
            when: user wants to change configuration
    """)
    policies = load_policies(str(_policies_file(tmp_path / "policies")))
    cfg = load_agent_from_dir(str(d), policies=policies)
    assert len(cfg.executors) == 1
    assert cfg.executors[0].executor_type == "configurator"


def test_executors_yaml_rejected_on_non_assistant_resident(tmp_path):
    """executors.yaml on butler (non-assistant resident) must fail load."""
    from agent_loader import LoadError, load_agent_from_dir
    from policies import load_policies
    try:
        from tests.test_agent_loader import _seed_resident, _policies_file
    except ImportError:
        from test_agent_loader import _seed_resident, _policies_file

    role = "butler"
    d = _seed_resident(tmp_path / "agents", role=role)
    _write(d / "executors.yaml", """\
        schema_version: 1
        executors:
          - executor_type: configurator
            purpose: x
            when: x
    """)
    policies = load_policies(str(_policies_file(tmp_path / "policies")))
    with pytest.raises(LoadError) as exc:
        load_agent_from_dir(str(d), policies=policies)
    msg = str(exc.value).lower()
    assert "executors.yaml" in msg
    assert "assistant" in msg


def test_executors_yaml_rejected_on_specialist(tmp_path):
    """executors.yaml on specialist must fail load (forbidden file)."""
    from agent_loader import LoadError, load_agent_from_dir
    try:
        from tests.test_agent_loader import _seed_specialist
    except ImportError:
        from test_agent_loader import _seed_specialist

    role = "finance"
    d = _seed_specialist(tmp_path / "agents", role=role)
    _write(d / "executors.yaml", """\
        schema_version: 1
        executors:
          - executor_type: configurator
            purpose: x
            when: x
    """)
    with pytest.raises(LoadError):
        load_agent_from_dir(str(d), policies=None)
