import importlib, pytest
pytestmark = [pytest.mark.unit]

def test_scope_registry_module_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scope_registry")

def test_memory_config_has_no_scope_fields():
    from config import MemoryConfig
    f = MemoryConfig.__dataclass_fields__
    assert "scopes_owned" not in f
    assert "scopes_readable" not in f
    assert "default_scope" not in f
