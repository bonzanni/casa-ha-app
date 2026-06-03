# tests/test_no_write_scope.py
"""The per-turn write_scope memory machinery is gone (tier model moved it to the reaper)."""
import inspect

import pytest

import agent as agent_mod
import session_registry

pytestmark = [pytest.mark.unit]


def test_session_registry_has_no_record_write_scope():
    assert not hasattr(session_registry.SessionRegistry, "record_write_scope")


def test_agent_module_source_drops_write_scope():
    src = inspect.getsource(agent_mod)
    assert "record_write_scope" not in src
    assert "write_scope" not in src  # no residual references on the turn path
