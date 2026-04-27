"""Tests for the <current_time> system-prompt block injected by Agent._process."""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

import pytest


def _compose_system_prompt_parts(base: str, memory_blocks: str, channel: str, channel_trust_display: str, tz):
    """Mirror of the system_parts assembly in agent.py::_process.

    This is what a future test of the live _process would assert on. For
    now we assert directly on a local compose — Task 3's implementation
    plants the same construction in agent.py.
    """
    from datetime import datetime
    parts = [base]
    if memory_blocks:
        parts.append("\n" + memory_blocks)
    parts.append(
        "\n<channel_context>\n"
        f"channel: {channel}\n"
        f"trust: {channel_trust_display}\n"
        "</channel_context>"
    )
    now = datetime.now(tz)
    parts.append(
        f"\n<current_time>\n"
        f"{now.isoformat(timespec='seconds')} "
        f"({now.strftime('%A').lower()} {now.strftime('%p').lower()}, "
        f"week {now.isocalendar().week})\n"
        f"</current_time>"
    )
    return "\n".join(parts)


class TestCurrentTimeBlock:
    def test_current_time_block_present_and_after_channel_context(self):
        tz = ZoneInfo("Europe/Amsterdam")
        out = _compose_system_prompt_parts(
            base="You are Ellen.",
            memory_blocks="",
            channel="telegram",
            channel_trust_display="authenticated",
            tz=tz,
        )
        assert "<current_time>" in out
        assert "</current_time>" in out
        assert out.index("<channel_context>") < out.index("<current_time>")

    def test_current_time_block_shape(self):
        tz = ZoneInfo("Europe/Amsterdam")
        out = _compose_system_prompt_parts(
            base="You are Ellen.",
            memory_blocks="",
            channel="telegram",
            channel_trust_display="authenticated",
            tz=tz,
        )
        m = re.search(
            r"<current_time>\n"
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} "
            r"\((monday|tuesday|wednesday|thursday|friday|saturday|sunday) "
            r"(am|pm), week \d{1,2}\)\n"
            r"</current_time>",
            out,
        )
        assert m is not None, f"shape mismatch: {out!r}"


class TestAgentProcessInjects:
    """Integration-style test: hit Agent._process via the production path."""

    @pytest.mark.asyncio
    async def test_process_emits_block_via_live_path(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock, patch

        import agent as agent_mod

        captured = {}

        class _FakeClient:
            def __init__(self, options):
                captured["options"] = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def query(self, text):
                return None

            async def receive_response(self):
                return
                yield  # pragma: no cover

            @property
            def session_id(self):
                return "sid"

        from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
        from bus import BusMessage, MessageType
        from channels import ChannelManager
        from mcp_registry import McpServerRegistry
        from session_registry import SessionRegistry
        from memory import NoOpMemory
        from scope_registry import ScopeRegistry, ScopeLibrary

        cfg = AgentConfig(
            role="assistant",
            model="claude-sonnet-4-6",
            system_prompt="You are Ellen.",
            character=CharacterConfig(name="Ellen"),
            tools=ToolsConfig(allowed=[]),
            memory=MemoryConfig(
                token_budget=0,
                scopes_readable=["personal"],
                scopes_owned=["personal"],
                default_scope="personal",
            ),
        )
        scope_lib = ScopeLibrary(scopes={})
        scope_reg = ScopeRegistry(scope_lib, threshold=0.35)
        monkeypatch.setattr(scope_reg, "filter_readable", lambda r, t: r)
        monkeypatch.setattr(scope_reg, "score", lambda q, s: [])
        monkeypatch.setattr(scope_reg, "active_from_scores",
                            lambda sc, d: [d] if d else [])
        # M4: agent._process now partitions readable into system/topical
        # via scope_registry.kind(); empty ScopeLibrary would raise.
        monkeypatch.setattr(scope_reg, "kind", lambda s: "topical")

        agent = agent_mod.Agent(
            config=cfg,
            memory=NoOpMemory(),
            session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
            mcp_registry=McpServerRegistry(),
            channel_manager=ChannelManager(),
            scope_registry=scope_reg,
        )

        with patch("agent.ClaudeSDKClient", _FakeClient):
            msg = BusMessage(
                type=MessageType.REQUEST, source="telegram",
                target="assistant", content="hello",
                channel="telegram", context={"chat_id": "42", "cid": "c-1"},
            )
            await agent._process(msg, on_token=None)

        system_prompt = captured["options"].system_prompt
        assert "<current_time>" in system_prompt
        assert "</current_time>" in system_prompt
