# tests/test_semantic_memory.py
"""SemanticMemory seam (spec §5): ABC contract + NoOp degraded impl."""
from __future__ import annotations

import pytest

from semantic_memory import NoOpSemanticMemory, SemanticMemory, render_mental_models, render_recall

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


def test_render_mental_models_empty() -> None:
    assert render_mental_models({"mental_models": []}) == ""
    assert render_mental_models({}) == ""


def test_render_mental_models_formats_entries() -> None:
    resp = {"mental_models": [
        {"content": "Nicola: terse, prefers metric units."},
        {"content": "Guest mode disables personal data."},
    ]}
    out = render_mental_models(resp)
    assert "terse" in out
    assert "Guest mode" in out
    assert "None" not in out


def test_render_mental_models_tolerates_alt_keys() -> None:
    assert "terse" in render_mental_models({"models": [{"content": "terse"}]})
    assert "terse" in render_mental_models({"items": [{"content": "terse"}]})


def test_render_recall_empty() -> None:
    assert render_recall({"results": []}) == ""
    assert render_recall({}) == ""


def test_render_recall_formats_facts() -> None:
    resp = {"results": [
        {"text": "Nicola keeps the thermostat at 20C.", "type": "world", "tags": ["house"]},
        {"text": "Nicola prefers terse replies.", "type": "observation", "tags": ["house"]},
    ]}
    out = render_recall(resp)
    assert "thermostat at 20C" in out
    assert "prefers terse replies" in out
    # one line per fact, no empty placeholder lines
    assert out.count("\n") <= 2
    assert "None" not in out
