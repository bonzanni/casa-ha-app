import importlib
import pytest

pytestmark = [pytest.mark.unit]


def test_memory_module_deleted():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("memory")
