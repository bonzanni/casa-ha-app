"""Unit tests for consult_other_agent_memory MCP tool (M6 Task 8, spec § 10.2).

Mirrors test_run_delegated_agent_memory.py's fixture pattern —
monkeypatch tools._agent_role_map / tools._specialist_registry and
agent.active_memory_provider, then exercise the tool.

The MCP tool returned object is wrapped as
``{"content": [{"type": "text", "text": json.dumps(payload)}]}`` by
``tools._result``; tests parse that JSON before asserting on payload
fields. Real call site is ``tool.handler(args)`` because ``@tool``
from claude_agent_sdk wraps the function as an ``SdkMcpTool``
dataclass — see ``tests/test_delegate_to_agent.py`` for the same
pattern.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeMemoryConfig:
    cross_peer_token_budget: int = 2000


@dataclass
class _FakeAgentConfig:
    role: str = "assistant"
    memory: _FakeMemoryConfig | None = None
    channels: list[str] | None = None

    def __post_init__(self) -> None:
        if self.memory is None:
            self.memory = _FakeMemoryConfig()
        if self.channels is None:
            self.channels = ["telegram"]


class _FakeProvider:
    """Records cross_peer_context call args; returns canned digest."""

    def __init__(self, return_value: str = "") -> None:
        self.calls: list[dict] = []
        self.return_value = return_value
        self.exc: Exception | None = None

    async def cross_peer_context(
        self,
        observer_role: str,
        query: str,
        tokens: int,
        user_peer: str = "nicola",
    ) -> str:
        self.calls.append({
            "observer_role": observer_role,
            "query": query,
            "tokens": tokens,
            "user_peer": user_peer,
        })
        if self.exc:
            raise self.exc
        return self.return_value


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def setup_tool(monkeypatch):
    """Wire up ``_agent_role_map``, ``_specialist_registry`` and
    ``agent.active_memory_provider``; stamp ``origin_var`` with an
    assistant origin so caller-role resolution succeeds.
    """
    import tools
    import agent as agent_mod

    fake_role_map = {"assistant": _FakeAgentConfig(role="assistant")}

    class _FakeSpecRegistry:
        _configs = {"finance": _FakeAgentConfig(role="finance", channels=[])}

        def get(self, name):
            return self._configs.get(name)

        def all_configs(self):
            # M6 polish (600a801): tool now uses public all_configs()
            return dict(self._configs)

    monkeypatch.setattr(
        tools, "_agent_role_map", fake_role_map, raising=False,
    )
    monkeypatch.setattr(
        tools, "_specialist_registry", _FakeSpecRegistry(), raising=False,
    )

    provider = _FakeProvider()
    monkeypatch.setattr(
        agent_mod, "active_memory_provider", provider, raising=False,
    )

    # Stamp origin_var so caller_role resolution sees "assistant".
    token = agent_mod.origin_var.set({"role": "assistant"})
    try:
        yield {
            "provider": provider,
            "tool": tools.consult_other_agent_memory,
        }
    finally:
        agent_mod.origin_var.reset(token)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_unknown_role_returns_error_string(setup_tool):
    """Spec § 6.2: unknown role → structured error payload, no exception."""
    tool = setup_tool["tool"]
    out = await tool.handler({"role": "no_such_role", "query": "anything"})
    payload = json.loads(out["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "unknown_role"
    assert "no_such_role" in payload["message"]


async def test_empty_query_returns_error_string(setup_tool):
    """Spec § 6.2: empty query → structured error payload."""
    tool = setup_tool["tool"]
    out = await tool.handler({"role": "finance", "query": ""})
    payload = json.loads(out["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "empty_query"
    assert "query is required" in payload["message"]


async def test_happy_path_wraps_provider_output_with_preamble(setup_tool):
    """Spec § 6.4: rendered context is wrapped with one-line preamble."""
    setup_tool["provider"].return_value = (
        "## What Finance knows about you (cross-role)\n"
        "- prioritizes Q2 invoicing"
    )
    tool = setup_tool["tool"]
    out = await tool.handler({"role": "finance", "query": "budget priorities"})
    payload = json.loads(out["content"][0]["text"])
    assert payload["status"] == "ok"
    body = payload["content"]
    assert 'Memory consult of finance on "budget priorities"' in body
    assert "## What Finance knows about you (cross-role)" in body
    assert "- prioritizes Q2 invoicing" in body


async def test_empty_provider_result_returns_no_memory_message(setup_tool):
    """Spec § 6.4: empty provider return → no-memory-found message."""
    setup_tool["provider"].return_value = ""
    tool = setup_tool["tool"]
    out = await tool.handler({"role": "finance", "query": "anything"})
    payload = json.loads(out["content"][0]["text"])
    assert payload["status"] == "ok"
    body = payload["content"]
    assert "No accumulated memory found for finance" in body
    assert '"anything"' in body


async def test_logs_consult_call_with_role_and_t_ms(setup_tool, caplog):
    """Spec § 6.5: tool emits consult_other_agent_memory_call info line."""
    setup_tool["provider"].return_value = "some content"
    tool = setup_tool["tool"]

    with caplog.at_level(logging.INFO, logger="tools"):
        await tool.handler({"role": "finance", "query": "x"})

    records = [
        r for r in caplog.records
        if r.message == "consult_other_agent_memory_call"
    ]
    assert len(records) == 1
    rec = records[0]
    assert getattr(rec, "role", None) == "finance"
    assert getattr(rec, "query_len", None) == 1
    assert getattr(rec, "result_len", None) > 0
    assert isinstance(getattr(rec, "t_ms", None), int)
