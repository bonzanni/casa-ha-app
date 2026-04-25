"""Tests for the delegate_to_agent framework tool (Phase 3.1)."""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, SessionConfig, ToolsConfig
from specialist_registry import (
    DelegationComplete,
    DelegationRecord,
    SpecialistRegistry,
)

pytestmark = pytest.mark.asyncio


def _seed_specialist_dir(
    base: Path, role: str = "finance", *, enabled: bool = True,
) -> Path:
    """Write a valid specialist directory under *base*. Returns the dir path."""
    d = base / role
    d.mkdir(parents=True)
    (d / "character.yaml").write_text(textwrap.dedent(f"""\
        schema_version: 1
        name: {role.capitalize()}
        role: {role}
        archetype: exec
        card: |
          x
        prompt: |
          x
    """), encoding="utf-8")
    (d / "voice.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (d / "response_shape.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (d / "runtime.yaml").write_text(textwrap.dedent(f"""\
        schema_version: 1
        model: sonnet
        enabled: {str(enabled).lower()}
        tools:
          allowed: [Read]
        memory:
          token_budget: 0
        session:
          strategy: ephemeral
    """), encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _specialist_cfg(role: str = "finance", enabled: bool = True) -> AgentConfig:
    return AgentConfig(
        role=role,
        model="claude-sonnet-4-6",
        system_prompt="You are " + role,
        character=CharacterConfig(name=role.capitalize()),
        enabled=enabled,
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=0),
        session=SessionConfig(strategy="ephemeral", idle_timeout=0),
    )


class _FakeSpecialistClient:
    """Minimal ClaudeSDKClient substitute for specialist turns.

    ``response_text`` is the text yielded by an AssistantMessage block.
    ``delay_s`` sleeps inside ``receive_response`` so timeout tests can
    drive the 60s degradation path without actually waiting 60s.
    """

    response_text: str = "finance reply"
    delay_s: float = 0.0
    raise_in_receive: Exception | None = None

    @classmethod
    def reset(cls, response="finance reply", delay=0.0, raise_exc=None):
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
        if _FakeSpecialistClient.delay_s > 0:
            await asyncio.sleep(_FakeSpecialistClient.delay_s)
        if _FakeSpecialistClient.raise_in_receive is not None:
            raise _FakeSpecialistClient.raise_in_receive

        # SDK shape has drifted: fields like AssistantMessage.model and
        # ResultMessage's positional args may be absent on older SDKs.
        # Mirror the `_mk_*` helpers in test_agent_process.py — try the
        # kwargs form, fall back to __new__ + attribute assignment.
        try:
            block = TextBlock(text=_FakeSpecialistClient.response_text)
        except TypeError:
            block = TextBlock(_FakeSpecialistClient.response_text)  # type: ignore[call-arg]
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

        reg = SpecialistRegistry(str(tmp_path / "ex"),
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
        """Disabled specialists are filtered at load-time — get() returns None,
        the tool cannot distinguish them from truly unknown names. Both
        paths collapse to kind=unknown_agent."""
        from tools import delegate_to_agent, init_tools

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=False)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        result = await _with_origin(
            delegate_to_agent.handler({
                "agent": "finance", "task": "x", "context": "", "mode": "sync",
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
    async def test_returns_specialist_text(self, tmp_path):
        from tools import delegate_to_agent, init_tools

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        _FakeSpecialistClient.reset(response="invoice drafted", delay=0)
        with patch("tools.ClaudeSDKClient", _FakeSpecialistClient):
            result = await _with_origin(
                delegate_to_agent.handler({
                    "agent": "finance", "task": "draft invoice",
                    "context": "lesina march",
                    "mode": "sync",
                }),
                _origin(),
            )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "ok"
        assert payload["agent"] == "finance"
        assert payload["text"] == "invoice drafted"
        assert "delegation_id" in payload
        assert payload["elapsed_s"] >= 0
        # Record was registered then cleaned up.
        assert not reg.has_delegation(payload["delegation_id"])


class TestSyncError:
    async def test_specialist_raises_is_reported_as_error(self, tmp_path):
        from tools import delegate_to_agent, init_tools

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        _FakeSpecialistClient.reset(raise_exc=RuntimeError("boom"))
        with patch("tools.ClaudeSDKClient", _FakeSpecialistClient):
            result = await _with_origin(
                delegate_to_agent.handler({
                    "agent": "finance", "task": "x", "context": "",
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

        reg = SpecialistRegistry(str(tmp_path / "ex"),
                                 tombstone_path=str(tmp_path / "del.json"))
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        # NOTE: not wrapped in _with_origin — origin_var stays None.
        result = await delegate_to_agent.handler({
            "agent": "finance", "task": "x", "context": "", "mode": "sync",
        })
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "error"
        assert payload["kind"] == "no_origin"


# ---------------------------------------------------------------------------
# TestTimeoutDegrades
# ---------------------------------------------------------------------------


class TestTimeoutDegrades:
    async def test_sync_over_timeout_returns_pending(
        self, tmp_path, monkeypatch,
    ):
        """A sync call whose specialist exceeds the 60s wait returns a
        pending marker. Here we monkeypatch the wait ceiling to 50ms
        so we don't actually wait 60s."""
        from tools import delegate_to_agent, init_tools
        import tools as tools_mod

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        bus.register("assistant", None)  # queue to receive the late NOTIFICATION
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        # Make the specialist body take "longer" than we wait.
        _FakeSpecialistClient.reset(response="eventual", delay=0.2)
        monkeypatch.setattr(tools_mod, "_SYNC_WAIT_TIMEOUT_S", 0.05)

        with patch("tools.ClaudeSDKClient", _FakeSpecialistClient):
            result = await _with_origin(
                delegate_to_agent.handler({
                    "agent": "finance", "task": "slow task",
                    "context": "", "mode": "sync",
                }),
                _origin(),
            )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "pending"
        assert payload["agent"] == "finance"
        assert "delegation_id" in payload

        # Let the background task finish so we don't leak it.
        await asyncio.sleep(0.3)

    async def test_degraded_path_eventually_posts_notification(
        self, tmp_path, monkeypatch,
    ):
        """After the pending return, the completion callback should post
        a NOTIFICATION to the delegator's bus queue."""
        from tools import delegate_to_agent, init_tools
        import tools as tools_mod

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        bus.register("assistant", None)  # Ellen's queue
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        _FakeSpecialistClient.reset(response="late result", delay=0.1)
        monkeypatch.setattr(tools_mod, "_SYNC_WAIT_TIMEOUT_S", 0.02)

        with patch("tools.ClaudeSDKClient", _FakeSpecialistClient):
            await _with_origin(
                delegate_to_agent.handler({
                    "agent": "finance", "task": "x", "context": "",
                    "mode": "sync",
                }),
                _origin(),
            )

        # Poll the queue briefly for the NOTIFICATION.
        found = None
        for _ in range(50):
            if not bus.queues["assistant"].empty():
                _pri, _seq, m = await bus.queues["assistant"].get()
                if m.type == MessageType.NOTIFICATION:
                    found = m
                    break
            await asyncio.sleep(0.02)
        assert found is not None
        assert isinstance(found.content, DelegationComplete)
        assert found.content.status == "ok"
        assert found.content.text == "late result"


# ---------------------------------------------------------------------------
# TestAsyncMode
# ---------------------------------------------------------------------------


class TestAsyncMode:
    async def test_returns_pending_immediately(self, tmp_path):
        from tools import delegate_to_agent, init_tools

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        bus.register("assistant", None)
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        _FakeSpecialistClient.reset(response="async reply", delay=0.05)
        with patch("tools.ClaudeSDKClient", _FakeSpecialistClient), \
             patch("tools.build_sdk_plugins", return_value=[]):
            t0 = asyncio.get_event_loop().time()
            result = await _with_origin(
                delegate_to_agent.handler({
                    "agent": "finance", "task": "x", "context": "",
                    "mode": "async",
                }),
                _origin(),
            )
            t1 = asyncio.get_event_loop().time()
            payload = json.loads(result["content"][0]["text"])
            assert payload["status"] == "pending"
            assert payload["mode"] == "async"
            # Returned without waiting for the specialist body.
            assert (t1 - t0) < 0.04

            # Wait for background completion (keep patch active so the
            # specialist task uses the fake, not the real SDK).
            await asyncio.sleep(0.15)

            # Verify a NOTIFICATION landed.
            assert not bus.queues["assistant"].empty()


# ---------------------------------------------------------------------------
# TestCancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    async def test_caller_cancel_cancels_specialist_task(self, tmp_path):
        """If the outer turn is cancelled (voice barge-in), the in-flight
        specialist task must be cancelled too — no NOTIFICATION posts."""
        from tools import delegate_to_agent, init_tools

        specialists = tmp_path / "ex"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        bus = MessageBus()
        bus.register("assistant", None)
        cm = ChannelManager()
        init_tools(cm, bus, reg)

        _FakeSpecialistClient.reset(response="slow", delay=1.0)

        async def _invoke():
            with patch("tools.ClaudeSDKClient", _FakeSpecialistClient):
                return await _with_origin(
                    delegate_to_agent.handler({
                        "agent": "finance", "task": "x", "context": "",
                        "mode": "sync",
                    }),
                    _origin(),
                )

        invocation = asyncio.create_task(_invoke())
        await asyncio.sleep(0.05)      # let it enter asyncio.wait
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation

        # No notifications posted — specialist was cancelled.
        await asyncio.sleep(0.05)
        assert bus.queues["assistant"].empty()


# ---------------------------------------------------------------------------
# TestMcpRegistryWiring — v0.6.1: specialist MCP servers resolved via registry
# ---------------------------------------------------------------------------


class TestMcpRegistryWiring:
    """`init_tools` accepts an optional `mcp_registry`; when passed,
    `_build_specialist_options` resolves `cfg.mcp_server_names` via the
    registry instead of hardcoding `mcp_servers={}`. This is the hook
    Phase 3.4 needs to make Alex's `n8n-workflows` + `casa-framework`
    tools available when he's flipped `enabled: true`."""

    async def test_mcp_registry_not_bound_degrades_to_empty(self, tmp_path):
        """Legacy 3-arg call — mcp_registry None — must not crash.
        Specialist options come back with empty mcp_servers."""
        from tools import _build_specialist_options, init_tools

        reg = SpecialistRegistry(str(tmp_path / "ex"),
                                 tombstone_path=str(tmp_path / "del.json"))
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg)  # no mcp_registry → None default

        cfg = _specialist_cfg(role="finance")
        cfg.mcp_server_names = ["n8n-workflows", "casa-framework"]
        options = _build_specialist_options(cfg)
        assert options.mcp_servers == {}

    async def test_mcp_registry_bound_resolves_to_registry_output(
        self, tmp_path,
    ):
        """When `mcp_registry` is passed, resolve() wins and its
        returned dict is passed straight through."""
        from mcp_registry import McpServerRegistry
        from tools import _build_specialist_options, init_tools

        mcp = McpServerRegistry()
        # Register a dummy SDK server so resolve() has something to return.
        mcp.register_sdk("casa-framework", {"type": "stdio", "command": "x"})

        reg = SpecialistRegistry(str(tmp_path / "ex"),
                                 tombstone_path=str(tmp_path / "del.json"))
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg, mcp)

        cfg = _specialist_cfg(role="finance")
        cfg.mcp_server_names = ["casa-framework"]
        options = _build_specialist_options(cfg)
        assert "casa-framework" in options.mcp_servers

    async def test_mcp_registry_bound_but_empty_names_yields_empty(
        self, tmp_path,
    ):
        """Specialist YAML with no `mcp_server_names` → empty mcp_servers,
        regardless of registry state. No exception."""
        from mcp_registry import McpServerRegistry
        from tools import _build_specialist_options, init_tools

        mcp = McpServerRegistry()
        mcp.register_sdk("casa-framework", {"type": "stdio", "command": "x"})

        reg = SpecialistRegistry(str(tmp_path / "ex"),
                                 tombstone_path=str(tmp_path / "del.json"))
        bus = MessageBus()
        cm = ChannelManager()
        init_tools(cm, bus, reg, mcp)

        cfg = _specialist_cfg(role="finance")
        cfg.mcp_server_names = []  # specialist declares no MCP deps
        options = _build_specialist_options(cfg)
        assert options.mcp_servers == {}


# ---------------------------------------------------------------------------
# TestMergedRoleMap — Task 7: delegate_to_agent resolves resident configs
# ---------------------------------------------------------------------------


class TestMergedRoleMap:
    async def test_delegate_to_agent_resolves_resident(self, tmp_path, monkeypatch):
        """delegate_to_agent(agent='butler', ...) finds a resident config."""
        import tools

        # Build a butler resident cfg using the existing helper
        resident_cfg = _specialist_cfg(role="butler")
        resident_cfg.character.name = "Tina"

        reg = SpecialistRegistry(
            str(tmp_path / "specs"),
            tombstone_path=str(tmp_path / "tombs.json"),
        )
        tools.init_tools(
            channel_manager=None,
            bus=None,
            specialist_registry=reg,
            mcp_registry=None,
            agent_role_map={"butler": resident_cfg},
        )

        import agent as agent_mod
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram", "chat_id": "1",
            "user_id": 1, "cid": "abc", "user_text": "x",
        })
        try:
            async def _fake_run(cfg, task_text, context_text):
                return f"Tina says ok: {task_text}"
            monkeypatch.setattr(tools, "_run_delegated_agent", _fake_run)

            result = await tools.delegate_to_agent.handler({
                "agent": "butler",
                "task": "turn off the lights",
                "context": "",
                "mode": "sync",
            })
            payload = json.loads(result["content"][0]["text"])
            assert payload["status"] == "ok"
            assert payload["agent"] == "butler"
            assert "Tina says ok" in payload["text"]
        finally:
            agent_mod.origin_var.reset(token)

    async def test_delegate_to_agent_unknown_returns_unknown_agent(
        self, tmp_path, monkeypatch,
    ):
        import tools

        reg = SpecialistRegistry(
            str(tmp_path / "specs"),
            tombstone_path=str(tmp_path / "tombs.json"),
        )
        tools.init_tools(
            channel_manager=None,
            bus=None,
            specialist_registry=reg,
            mcp_registry=None,
            agent_role_map={},
        )

        import agent as agent_mod
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram", "chat_id": "1",
            "user_id": 1, "cid": "abc", "user_text": "x",
        })
        try:
            result = await tools.delegate_to_agent.handler({
                "agent": "ghost",
                "task": "anything",
                "context": "",
                "mode": "sync",
            })
            payload = json.loads(result["content"][0]["text"])
            assert payload["status"] == "error"
            assert payload["kind"] == "unknown_agent"
        finally:
            agent_mod.origin_var.reset(token)

    async def test_delegate_to_agent_interactive_rejected_for_resident(
        self, tmp_path, monkeypatch,
    ):
        import tools, agent as agent_mod

        resident_cfg = _specialist_cfg(role="butler")
        resident_cfg.character.name = "Tina"
        resident_cfg.channels = ["voice"]   # marker that it's a resident

        spec_reg = SpecialistRegistry(
            specialists_dir=str(tmp_path / "specs"),
            tombstone_path=str(tmp_path / "tombs.json"),
        )
        tools.init_tools(
            channel_manager=None,
            bus=None,
            specialist_registry=spec_reg,
            mcp_registry=None,
            agent_role_map={"butler": resident_cfg},
        )
        token = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram", "chat_id": "1",
            "user_id": 1, "cid": "abc", "user_text": "x",
        })
        try:
            result = await tools.delegate_to_agent.handler({
                "agent": "butler",
                "task": "x",
                "context": "",
                "mode": "interactive",
            })
            payload = json.loads(result["content"][0]["text"])
            assert payload["status"] == "error"
            assert payload["kind"] == "interactive_not_supported"
        finally:
            agent_mod.origin_var.reset(token)

    async def test_delegate_to_agent_depth_cap(self, tmp_path, monkeypatch):
        """A nested delegate_to_agent (depth >= 1) returns delegation_depth_exceeded."""
        import tools, agent as agent_mod

        resident_cfg = _specialist_cfg(role="butler")
        resident_cfg.character.name = "Tina"
        resident_cfg.channels = ["voice"]

        from specialist_registry import SpecialistRegistry
        spec_reg = SpecialistRegistry(
            specialists_dir=str(tmp_path / "specs"),
            tombstone_path=str(tmp_path / "tombs.json"),
        )
        tools.init_tools(
            channel_manager=None,
            bus=None,
            specialist_registry=spec_reg,
            mcp_registry=None,
            agent_role_map={"butler": resident_cfg},
        )
        # Set origin AT depth=1 (simulating that we are already inside a
        # delegated turn).
        token = agent_mod.origin_var.set({
            "role": "butler", "channel": "telegram", "chat_id": "1",
            "user_id": 1, "cid": "abc", "user_text": "x",
            "delegation_depth": 1,
        })
        try:
            result = await tools.delegate_to_agent.handler({
                "agent": "butler",
                "task": "nested",
                "context": "",
                "mode": "sync",
            })
            payload = json.loads(result["content"][0]["text"])
            assert payload["status"] == "error"
            assert payload["kind"] == "delegation_depth_exceeded"
        finally:
            agent_mod.origin_var.reset(token)
