# tests/test_semantic_memory_resolve.py
"""Semantic-memory backend resolution + factory (spec §5/§7).
Independent of the legacy MemoryProvider resolution."""
from __future__ import annotations

import pytest

from casa_core import build_semantic_memory, resolve_semantic_memory_choice
from hindsight_memory import HindsightSemanticMemory
from semantic_memory import NoOpSemanticMemory

pytestmark = [pytest.mark.unit]


def test_hindsight_when_selected_with_url() -> None:
    choice = resolve_semantic_memory_choice(
        {"MEMORY_BACKEND": "hindsight", "HINDSIGHT_API_URL": "http://5884eb17-hindsight:8888"}
    )
    assert choice.backend == "hindsight"
    assert choice.base_url == "http://5884eb17-hindsight:8888"
    assert isinstance(build_semantic_memory(choice), HindsightSemanticMemory)


def test_hindsight_without_url_raises() -> None:
    with pytest.raises(ValueError):
        resolve_semantic_memory_choice({"MEMORY_BACKEND": "hindsight"})


def test_noop_default() -> None:
    choice = resolve_semantic_memory_choice({})
    assert choice.backend == "noop"
    assert isinstance(build_semantic_memory(choice), NoOpSemanticMemory)
