import pytest, casa_core, agent
pytestmark = [pytest.mark.unit]

def test_legacy_backend_machinery_gone():
    assert not hasattr(casa_core, "resolve_memory_backend_choice")
    assert not hasattr(casa_core, "_wrap_memory_for_strategy")
    assert not hasattr(casa_core, "_MemoryChoice")

def test_active_memory_provider_field_gone():
    assert not hasattr(agent, "active_memory_provider")

def test_semantic_choice_invalid_backend_is_noop():
    c = casa_core.resolve_semantic_memory_choice({"MEMORY_BACKEND": "bogus"})
    assert c.backend == "noop"

def test_semantic_choice_noop_when_unset():
    assert casa_core.resolve_semantic_memory_choice({}).backend == "noop"
