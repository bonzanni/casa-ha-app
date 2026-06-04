"""Guard: the dead legacy `memory` param is gone from Agent.__init__ (retire 3/6)."""
import inspect

import pytest

from agent import Agent

pytestmark = [pytest.mark.unit]


def test_agent_init_has_no_memory_param():
    assert "memory" not in inspect.signature(Agent.__init__).parameters
