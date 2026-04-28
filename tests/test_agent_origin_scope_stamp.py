"""M2.G6 — agent must stamp the read-path argmax scope onto origin_var
after `active` is computed, so engage_executor (and any other tool that
reads origin.scope) gets the engager's actual rooted scope rather than
the literal "meta" fallback used by tools.py:1558.

The test follows the existing test_agent_process_scope.py pattern:
real Agent + FakeClient SDK substitute. We capture origin_var inside
the per-scope ensure_session call, which fires AFTER the read-path
classifier has computed `active` (and AFTER the M2.G6 origin re-set).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from agent import Agent
from bus import BusMessage, MessageType
from config import (
    AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig,
)

from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# SDK helpers (mirror of test_agent_process_scope.py)
# ---------------------------------------------------------------------------


def _mk_text_block(text: str) -> _SDKTextBlock:
    try:
        return _SDKTextBlock(text=text)
    except TypeError:
        return _SDKTextBlock(text)  # type: ignore[call-arg]


def _mk_assistant(text: str) -> _SDKAssistantMessage:
    block = _mk_text_block(text)
    try:
        return _SDKAssistantMessage(content=[block])
    except TypeError:
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = [block]  # type: ignore[attr-defined]
        return m


def _mk_result(sid: str) -> _SDKResultMessage:
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid  # type: ignore[attr-defined]
    return m


class FakeClient:
    """Minimal ClaudeSDKClient substitute that yields one assistant turn."""

    def __init__(self, options):
        self._options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def query(self, text):
        self._last = text

    async def receive_response(self):
        yield _mk_assistant("ok")
        yield _mk_result("sdk-sid-1")


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


async def test_origin_var_carries_argmax_scope_after_compute():
    """When the read-path classifier scores `finance` highest, origin_var
    inside the same _process turn must carry scope=finance."""
    import agent as agent_mod

    captured: dict = {}

    async def capture_origin(*, session_id, agent_role, user_peer):
        # Fired during _one_scope which runs AFTER `active` is computed
        # AFTER the M2.G6 origin_var re-set we're testing.
        snapshot = agent_mod.origin_var.get(None) or {}
        captured["origin"] = dict(snapshot)
        return None

    memory = Mock()
    memory.ensure_session = AsyncMock(side_effect=capture_origin)
    memory.get_context = AsyncMock(return_value="")
    memory.add_turn = AsyncMock()

    # Stub scope_registry — finance dominates.
    reg = Mock()
    reg.filter_readable = Mock(return_value=["finance", "domestic"])
    reg.score = Mock(return_value={"finance": 0.92, "domestic": 0.10})
    reg.active_from_scores = Mock(return_value=["finance"])
    reg.argmax_scope = Mock(return_value="finance")
    reg.cache_stats = Mock(return_value=(0, 1))
    reg.threshold = 0.5

    cfg = AgentConfig(
        role="assistant",
        model="claude-sonnet-4-6",
        system_prompt="you are ellen",
        character=CharacterConfig(name="Ellen"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(
            token_budget=900,
            read_strategy="per_turn",
            scopes_owned=["domestic"],
            scopes_readable=["finance", "domestic"],
            default_scope="domestic",
        ),
    )

    agent = Agent(
        config=cfg,
        memory=memory,
        session_registry=Mock(
            get=Mock(return_value=None),
            touch=AsyncMock(),
            register=AsyncMock(),
        ),
        mcp_registry=Mock(resolve=Mock(return_value={})),
        channel_manager=Mock(),
        scope_registry=reg,
    )

    msg = BusMessage(
        type=MessageType.CHANNEL_IN,
        source="telegram",
        target="assistant",
        content="how much did we spend on groceries",
        channel="telegram",
        context={"chat_id": "123", "user_id": "nicola"},
    )

    with patch("agent.ClaudeSDKClient", FakeClient):
        await agent._process(msg)

    # The capture fires inside _one_scope which runs AFTER our re-set.
    assert "scope" in captured.get("origin", {}), (
        "_process did not stamp 'scope' onto origin_var after computing "
        f"active scopes. captured={captured.get('origin')!r}"
    )
    assert captured["origin"]["scope"] == "finance"
