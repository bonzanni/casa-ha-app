"""Unit tests for consult_other_agent_memory MCP tool (spec § 4.3).

Re-pointed from MemoryProvider.cross_peer_context to
SemanticMemory.cross_recall via active_semantic_memory (load-model Task 2).

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
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSpecRegistry:
    """Minimal stand-in for SpecialistRegistry used in role-validation tests."""

    _configs: dict = {}
    _disabled: set = set()

    def __init__(
        self,
        configs: dict | None = None,
        disabled: set | None = None,
    ) -> None:
        self._configs = configs or {}
        self._disabled = disabled or set()

    def get(self, name: str):
        return self._configs.get(name)

    def all_configs(self):
        return dict(self._configs)

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled

    def disabled_roles(self) -> list[str]:
        return sorted(self._disabled)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def setup_tool(monkeypatch):
    """Wire up ``_agent_role_map``, ``_specialist_registry`` and
    ``agent.active_semantic_memory``; stamp ``origin_var`` with an
    assistant origin so caller-role resolution succeeds.
    """
    import tools
    import agent as agent_mod

    finance_cfg = SimpleNamespace(
        role="finance",
        memory=SimpleNamespace(cross_peer_token_budget=2000),
        channels=[],
    )
    assistant_cfg = SimpleNamespace(
        role="assistant",
        memory=SimpleNamespace(cross_peer_token_budget=2000),
        channels=["telegram"],
    )

    fake_role_map = {
        "assistant": assistant_cfg,
        "finance": finance_cfg,
    }

    registry = _FakeSpecRegistry(
        configs={"finance": finance_cfg},
        disabled={"health"},
    )

    monkeypatch.setattr(tools, "_agent_role_map", fake_role_map, raising=False)
    monkeypatch.setattr(tools, "_specialist_registry", registry, raising=False)

    sem = AsyncMock()
    sem.cross_recall.return_value = ""
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)

    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram"})
    try:
        yield {
            "sem": sem,
            "tool": tools.consult_other_agent_memory,
        }
    finally:
        agent_mod.origin_var.reset(token)


# ---------------------------------------------------------------------------
# Core happy-path tests
# ---------------------------------------------------------------------------


async def test_known_role_calls_cross_recall_with_correct_bank(setup_tool):
    """Known role → cross_recall invoked with bank ``"casa-finance"``."""
    sem = setup_tool["sem"]
    sem.cross_recall.return_value = "- Budget is tight."

    tool = setup_tool["tool"]
    out = await tool.handler({"role": "finance", "query": "budget priorities"})
    payload = json.loads(out["content"][0]["text"])

    assert payload["status"] == "ok"
    # Bank must be target role's bank — "casa-finance"
    assert sem.cross_recall.await_args.args[0] == "casa-finance"
    # Rendered text propagates to result
    assert "- Budget is tight." in payload["content"]


async def test_known_role_wraps_output_with_preamble(setup_tool):
    """Spec § 6.4: rendered context is wrapped with one-line preamble."""
    setup_tool["sem"].cross_recall.return_value = (
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


async def test_empty_cross_recall_result_returns_no_memory_message(setup_tool):
    """Spec § 6.4: empty cross_recall return → no-memory-found message."""
    setup_tool["sem"].cross_recall.return_value = ""
    tool = setup_tool["tool"]
    out = await tool.handler({"role": "finance", "query": "anything"})
    payload = json.loads(out["content"][0]["text"])

    assert payload["status"] == "ok"
    body = payload["content"]
    assert "No accumulated memory found for finance" in body
    assert '"anything"' in body


async def test_sem_none_returns_graceful_empty(monkeypatch):
    """If active_semantic_memory is None, graceful empty (not wired)."""
    import tools
    import agent as agent_mod

    finance_cfg = SimpleNamespace(
        role="finance",
        memory=SimpleNamespace(cross_peer_token_budget=2000),
        channels=[],
    )
    monkeypatch.setattr(
        tools, "_agent_role_map", {"assistant": SimpleNamespace(
            role="assistant",
            memory=SimpleNamespace(cross_peer_token_budget=2000),
            channels=["telegram"],
        )}, raising=False,
    )
    monkeypatch.setattr(
        tools, "_specialist_registry",
        _FakeSpecRegistry(configs={"finance": finance_cfg}),
        raising=False,
    )
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None, raising=False)

    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram"})
    try:
        tool = tools.consult_other_agent_memory
        out = await tool.handler({"role": "finance", "query": "budget"})
        payload = json.loads(out["content"][0]["text"])
        assert payload["status"] == "ok"
        # Content is the empty/no-memory message, not a crash
        assert "No accumulated memory found" in payload["content"]
    finally:
        agent_mod.origin_var.reset(token)


# ---------------------------------------------------------------------------
# Validation tests (unchanged behaviour)
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


async def test_unknown_role_error_message_lists_disabled_roles(setup_tool):
    """Spec § 3.2.1: disabled roles ARE consultable; the unknown_role
    error must list them so the model can self-correct."""
    tool = setup_tool["tool"]
    out = await tool.handler({"role": "totally_made_up", "query": "x"})
    payload = json.loads(out["content"][0]["text"])

    assert payload["status"] == "error"
    # 'health' is disabled but listed in available roles for visibility.
    assert "health" in payload["message"]


async def test_unknown_role_still_returns_error_not_fall_through(setup_tool):
    """Regression: a role that's neither registered nor disabled still
    returns unknown_role. Prevents the fall-through from eating typos."""
    tool = setup_tool["tool"]
    out = await tool.handler({"role": "totally_made_up", "query": "anything"})
    payload = json.loads(out["content"][0]["text"])

    assert payload["status"] == "error"
    assert payload["kind"] == "unknown_role"


# ---------------------------------------------------------------------------
# Phase 5 / E-15 — disabled-peer fall-through
# ---------------------------------------------------------------------------


async def test_disabled_peer_falls_through_to_cross_recall(setup_tool):
    """Spec § 3.2.1: a disabled-but-known specialist's memory is
    consultable. Tool falls through to cross_recall instead of
    returning unknown_role."""
    sem = setup_tool["sem"]
    sem.cross_recall.return_value = (
        "## What Health knows about you (cross-role)\n"
        "- exercises 4x a week"
    )
    tool = setup_tool["tool"]
    out = await tool.handler({"role": "health", "query": "fitness goals"})
    payload = json.loads(out["content"][0]["text"])

    # Status is OK, not error — fall-through worked.
    assert payload["status"] == "ok"
    body = payload["content"]
    assert 'Memory consult of health on "fitness goals"' in body
    assert "## What Health knows about you (cross-role)" in body

    # cross_recall was invoked with the disabled role's bank.
    assert sem.cross_recall.await_args.args[0] == "casa-health"


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


async def test_logs_consult_call_with_role_and_t_ms(setup_tool, caplog):
    """Spec § 6.5: tool emits consult_other_agent_memory_call info line."""
    setup_tool["sem"].cross_recall.return_value = "some content"
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


# ---------------------------------------------------------------------------
# cross_recall keyword args
# ---------------------------------------------------------------------------


async def test_cross_recall_called_with_max_tokens_and_budget_low(setup_tool):
    """cross_recall must receive max_tokens from cross_peer_token_budget
    and budget='low' (cross reads are cheap, spec § 4.3)."""
    sem = setup_tool["sem"]
    sem.cross_recall.return_value = "data"
    tool = setup_tool["tool"]

    await tool.handler({"role": "finance", "query": "test"})

    call_kwargs = sem.cross_recall.await_args.kwargs
    assert call_kwargs.get("max_tokens") == 2000
    assert call_kwargs.get("budget") == "low"
