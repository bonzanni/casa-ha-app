"""consult_other_agent_memory is retired; cross_recall removed from the seam (plan 3, Task 7)."""
import pytest

import tools
from hindsight_memory import HindsightSemanticMemory
from semantic_memory import NoOpSemanticMemory, SemanticMemory

pytestmark = [pytest.mark.unit]


def test_consult_tool_is_gone():
    assert not hasattr(tools, "consult_other_agent_memory")


def test_consult_not_in_casa_tools():
    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in tools.CASA_TOOLS}
    assert "consult_other_agent_memory" not in names


def test_cross_recall_removed_from_seam():
    assert not hasattr(SemanticMemory, "cross_recall")
    assert not hasattr(NoOpSemanticMemory, "cross_recall")
    assert not hasattr(HindsightSemanticMemory, "cross_recall")
