# tests/test_semantic_memory.py
"""SemanticMemory seam (spec §5): ABC contract + NoOp degraded impl."""
from __future__ import annotations

import pytest

from semantic_memory import NoOpSemanticMemory, SemanticMemory

pytestmark = [pytest.mark.unit]


def test_noop_is_semantic_memory() -> None:
    assert issubclass(NoOpSemanticMemory, SemanticMemory)


def test_semantic_memory_is_abstract() -> None:
    # The seam is an ABC: the abstract methods must prevent direct instantiation
    # (guards against an @abstractmethod being dropped from any of the four).
    with pytest.raises(TypeError):
        SemanticMemory()


async def test_noop_retain_is_silent() -> None:
    mem = NoOpSemanticMemory()
    assert await mem.retain("casa-assistant", [{"content": "x"}]) is None


async def test_noop_reads_return_empty_string() -> None:
    mem = NoOpSemanticMemory()
    assert await mem.recall("casa-assistant", "q", tags=["house"], max_tokens=512) == ""
    assert await mem.profile("casa-assistant") == ""
    assert await mem.cross_recall("casa-butler", "q", max_tokens=256) == ""
