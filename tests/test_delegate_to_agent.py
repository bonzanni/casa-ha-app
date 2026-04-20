"""Tests for the delegate_to_agent framework tool (Phase 3.1)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from config import AgentConfig, MemoryConfig, SessionConfig, ToolsConfig
from executor_registry import (
    DelegationComplete,
    DelegationRecord,
    ExecutorRegistry,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _executor_cfg(role: str = "alex", enabled: bool = True) -> AgentConfig:
    return AgentConfig(
        name=role.capitalize(),
        role=role,
        model="claude-sonnet-4-6",
        personality="You are " + role,
        enabled=enabled,
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=0),
        session=SessionConfig(strategy="ephemeral", idle_timeout=0),
    )


class _FakeExecutorClient:
    """Minimal ClaudeSDKClient substitute for executor turns.

    ``response_text`` is the text yielded by an AssistantMessage block.
    ``delay_s`` sleeps inside ``receive_response`` so timeout tests can
    drive the 60s degradation path without actually waiting 60s.
    """

    response_text: str = "alex reply"
    delay_s: float = 0.0
    raise_in_receive: Exception | None = None

    @classmethod
    def reset(cls, response="alex reply", delay=0.0, raise_exc=None):
        cls.response_text = response
        cls.delay_s = delay
        cls.raise_in_receive = raise_exc

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def query(self, text):
        self._text = text

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage, ResultMessage, TextBlock, SystemMessage,
        )
        if _FakeExecutorClient.delay_s > 0:
            await asyncio.sleep(_FakeExecutorClient.delay_s)
        if _FakeExecutorClient.raise_in_receive is not None:
            raise _FakeExecutorClient.raise_in_receive

        # SDK shape has drifted: fields like AssistantMessage.model and
        # ResultMessage's positional args may be absent on older SDKs.
        # Mirror the `_mk_*` helpers in test_agent_process.py — try the
        # kwargs form, fall back to __new__ + attribute assignment.
        try:
            block = TextBlock(text=_FakeExecutorClient.response_text)
        except TypeError:
            block = TextBlock(_FakeExecutorClient.response_text)  # type: ignore[call-arg]
        try:
            sys_msg = SystemMessage(
                subtype="init", data={"session_id": "exec-sid"},
            )
        except TypeError:
            sys_msg = SystemMessage.__new__(SystemMessage)
            sys_msg.subtype = "init"  # type: ignore[attr-defined]
            sys_msg.data = {"session_id": "exec-sid"}  # type: ignore[attr-defined]
        yield sys_msg
        try:
            asst = AssistantMessage(content=[block])
        except TypeError:
            asst = AssistantMessage.__new__(AssistantMessage)
            asst.content = [block]  # type: ignore[attr-defined]
        yield asst
        try:
            result = ResultMessage(session_id="exec-sid")
        except TypeError:
            result = ResultMessage.__new__(ResultMessage)
            result.session_id = "exec-sid"  # type: ignore[attr-defined]
        yield result


async def _with_origin(coro, origin: dict[str, Any]):
    """Run *coro* with origin_var pre-set, emulating an in-turn call."""
    import agent as agent_mod
    token = agent_mod.origin_var.set(origin)
    try:
        return await coro
    finally:
        agent_mod.origin_var.reset(token)


def _origin(role="assistant", channel="telegram", chat_id="x"):
    return {
        "role": role,
        "channel": channel,
        "chat_id": chat_id,
        "cid": "c1",
        "user_text": "please do X",
    }


# ---------------------------------------------------------------------------
# TestUnknownAgent / TestDisabledAgent
# ---------------------------------------------------------------------------


class TestUnknownAgent:
    async def test_returns_error_content(self, tmp_path):
        from tools import delegate_to_agent, init_tools

        reg = ExecutorRegistry(str(tmp_path / "ex"),
                               tombstone_path=str(tmp_path / "del.json"))
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        result = await _with_origin(
            delegate_to_agent.handler({
                "agent": "ghost", "task": "x", "context": "", "mode": "sync",
            }),
            _origin(),
        )
        assert "content" in result
        text = result["content"][0]["text"]
        payload = json.loads(text)
        assert payload["status"] == "error"
        assert payload["kind"] == "unknown_agent"


class TestDisabledAgent:
    async def test_returns_unknown_agent_error(self, tmp_path):
        """Disabled executors are filtered at load-time — get() returns None,
        the tool cannot distinguish them from truly unknown names. Both
        paths collapse to kind=unknown_agent."""
        from tools import delegate_to_agent, init_tools

        executors = tmp_path / "ex"
        executors.mkdir()
        (executors / "alex.yaml").write_text(
            "name: Alex\nrole: alex\nmodel: sonnet\npersonality: a\n"
            "enabled: false\n"
            "memory:\n  token_budget: 0\n"
            "session:\n  strategy: ephemeral\n  idle_timeout: 0\n",
            encoding="utf-8",
        )
        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        result = await _with_origin(
            delegate_to_agent.handler({
                "agent": "alex", "task": "x", "context": "", "mode": "sync",
            }),
            _origin(),
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "unknown_agent"


# ---------------------------------------------------------------------------
# TestSyncOk / TestSyncError
# ---------------------------------------------------------------------------


class TestSyncOk:
    async def test_returns_executor_text(self, tmp_path):
        from tools import delegate_to_agent, init_tools

        executors = tmp_path / "ex"
        executors.mkdir()
        (executors / "alex.yaml").write_text(
            "name: Alex\nrole: alex\nmodel: sonnet\npersonality: a\n"
            "enabled: true\n"
            "memory:\n  token_budget: 0\n"
            "session:\n  strategy: ephemeral\n  idle_timeout: 0\n",
            encoding="utf-8",
        )
        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        _FakeExecutorClient.reset(response="invoice drafted", delay=0)
        with patch("tools.ClaudeSDKClient", _FakeExecutorClient):
            result = await _with_origin(
                delegate_to_agent.handler({
                    "agent": "alex", "task": "draft invoice",
                    "context": "lesina march",
                    "mode": "sync",
                }),
                _origin(),
            )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "ok"
        assert payload["agent"] == "alex"
        assert payload["text"] == "invoice drafted"
        assert "delegation_id" in payload
        assert payload["elapsed_s"] >= 0
        # Record was registered then cleaned up.
        assert not reg.has_delegation(payload["delegation_id"])


class TestSyncError:
    async def test_executor_raises_is_reported_as_error(self, tmp_path):
        from tools import delegate_to_agent, init_tools

        executors = tmp_path / "ex"
        executors.mkdir()
        (executors / "alex.yaml").write_text(
            "name: Alex\nrole: alex\nmodel: sonnet\npersonality: a\n"
            "enabled: true\n"
            "memory:\n  token_budget: 0\n"
            "session:\n  strategy: ephemeral\n  idle_timeout: 0\n",
            encoding="utf-8",
        )
        reg = ExecutorRegistry(str(executors),
                               tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        _FakeExecutorClient.reset(raise_exc=RuntimeError("boom"))
        with patch("tools.ClaudeSDKClient", _FakeExecutorClient):
            result = await _with_origin(
                delegate_to_agent.handler({
                    "agent": "alex", "task": "x", "context": "",
                    "mode": "sync",
                }),
                _origin(),
            )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert "delegation_id" in payload
        assert "kind" in payload
        # Record was cleaned up.
        assert not reg.has_delegation(payload["delegation_id"])


# ---------------------------------------------------------------------------
# TestOriginMissing
# ---------------------------------------------------------------------------


class TestOriginMissing:
    async def test_no_origin_returns_error(self, tmp_path):
        """Called outside a turn (origin_var unset) — shouldn't happen
        in prod but must not crash. Return error, do not dispatch."""
        from tools import delegate_to_agent, init_tools

        reg = ExecutorRegistry(str(tmp_path / "ex"),
                               tombstone_path=str(tmp_path / "del.json"))
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        # NOTE: not wrapped in _with_origin — origin_var stays None.
        result = await delegate_to_agent.handler({
            "agent": "alex", "task": "x", "context": "", "mode": "sync",
        })
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "no_origin"
