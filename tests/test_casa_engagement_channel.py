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
def channel_server(monkeypatch):
    """Force a fresh re-import of the channel server module per test.

    The module reads ``--engagement-id`` from ``sys.argv`` at import time
    (via the pytest-friendly bottom block). We seed argv before importing
    so ENGAGEMENT_ID is populated without entering the stdio run loop.
    """
    monkeypatch.setattr(
        sys, "argv",
        ["casa_engagement_channel", "--engagement-id", "deadbeef-test"],
    )
    sys.modules.pop("channels.casa_engagement_channel", None)
    module = importlib.import_module("channels.casa_engagement_channel")
    assert module.ENGAGEMENT_ID == "deadbeef-test"
    yield module
    sys.modules.pop("channels.casa_engagement_channel", None)


class TestChannelServerSkeleton:
    async def test_reply_tool_registered(self, channel_server):
        tools = await channel_server.list_tools()
        names = [t.name for t in tools]
        assert "reply" in names

    async def test_capability_includes_claude_channel(self, channel_server):
        caps = channel_server.declared_capabilities()
        assert isinstance(caps, dict)
        assert "claude/channel" in caps

    async def test_reply_forwards_to_internal_socket(
        self, channel_server, monkeypatch,
    ):
        captured: list[tuple[str, dict]] = []

        async def fake_post(path, payload):
            captured.append((path, dict(payload)))
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        result = await channel_server.invoke_tool(
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

        await channel_server.invoke_tool(
            "reply", {"chat_id": "11111", "text": "one"},
        )
        await channel_server.invoke_tool(
            "reply", {"chat_id": "99999", "text": "two"},
        )

        assert len(captured) == 2
        for payload in captured:
            assert "chat_id" not in payload
            assert payload.get("engagement_id") == "deadbeef-test"
        assert captured[0]["text"] == "one"
        assert captured[1]["text"] == "two"
