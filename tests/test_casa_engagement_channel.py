"""Tests for the casa-engagement-channel MCP server skeleton (v0.37.0 Phase 1).

These tests cover the Phase 1 surface: a single ``reply`` tool that POSTs to
``/internal/channel/send_to_topic`` on the casa-main Unix socket, plus
``declared_capabilities()`` returning the ``claude/channel`` experimental
capability that the bootstrap injects into MCP initialization options.

The D2 contract locked behavior: ``reply`` accepts a ``chat_id`` arg for SDK
compatibility but never forwards it. Routing always uses the engagement_id
captured at module import time from the ``--engagement-id`` CLI flag.
"""

from __future__ import annotations

import importlib
import sys

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def channel_server():
    """Force a fresh re-import of the channel server module per test.

    Uses the explicit ``_configure_for_test`` seam to populate
    ``ENGAGEMENT_ID`` directly, avoiding argv-driven magic.
    """
    sys.modules.pop("channels.casa_engagement_channel", None)
    module = importlib.import_module("channels.casa_engagement_channel")
    module._configure_for_test("deadbeef-test")
    assert module.ENGAGEMENT_ID == "deadbeef-test"
    yield module
    sys.modules.pop("channels.casa_engagement_channel", None)


class TestChannelServerSkeleton:
    async def test_reply_tool_registered(self, channel_server):
        tools = await channel_server._list_tools_for_tests()
        names = [t.name for t in tools]
        assert "reply" in names

    async def test_capability_includes_claude_channel(self, channel_server):
        caps = channel_server.declared_capabilities()
        assert isinstance(caps, dict)
        assert "claude/channel" in caps

    async def test_capability_includes_claude_channel_permission(self, channel_server):
        """Phase 2 (Task 17): claude/channel/permission added to capabilities."""
        caps = channel_server.declared_capabilities()
        assert "claude/channel/permission" in caps

    async def test_initialization_options_carry_experimental_capabilities(
        self, channel_server,
    ):
        """End-to-end (§A.6.1): the bootstrap injects experimental caps
        into the inner Server's InitializationOptions."""
        low_level = channel_server._resolve_mcp_server()
        assert low_level is not None
        opts = low_level.create_initialization_options(
            experimental_capabilities=channel_server.declared_capabilities(),
        )
        experimental = opts.capabilities.experimental or {}
        assert "claude/channel" in experimental
        assert "claude/channel/permission" in experimental

    async def test_reply_forwards_to_internal_socket(
        self, channel_server, monkeypatch,
    ):
        captured: list[tuple[str, dict]] = []

        async def fake_post(path, payload):
            captured.append((path, dict(payload)))
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        result = await channel_server._invoke_tool_for_tests(
            "reply", {"chat_id": "ignored", "text": "hello"},
        )

        assert len(captured) == 1
        path, payload = captured[0]
        assert path == "/internal/channel/send_to_topic"
        assert payload.get("engagement_id") == "deadbeef-test"
        assert payload.get("text") == "hello"
        assert result == {"ok": True}

    async def test_reply_d2_ignores_chat_id_arg(
        self, channel_server, monkeypatch,
    ):
        captured: list[dict] = []

        async def fake_post(path, payload):
            captured.append(dict(payload))
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        await channel_server._invoke_tool_for_tests(
            "reply", {"chat_id": "11111", "text": "one"},
        )
        await channel_server._invoke_tool_for_tests(
            "reply", {"chat_id": "99999", "text": "two"},
        )

        assert len(captured) == 2
        for payload in captured:
            assert "chat_id" not in payload
            assert payload.get("engagement_id") == "deadbeef-test"
        assert captured[0]["text"] == "one"
        assert captured[1]["text"] == "two"
