# tests/test_channel_internal_handlers.py
"""Unit tests for channels.channel_handlers (v0.37.0 Phase 1).

Covers ``POST /internal/channel/send_to_topic``: the casa-main side of the
bridge that ``casa_engagement_channel.py`` (the stdio MCP server inside each
``claude_code`` engagement) POSTs into over ``/run/casa/internal.sock``.

Phase 1 surface is intentionally one path; later phases extend the dict
returned by ``_make_channel_handlers`` (see spec §A.3).
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeChannel:
    """Capture-fake for ``TelegramChannel.send_to_topic``.

    Returns incrementing ``message_id`` values starting at 7000 so tests can
    assert the handler propagates the value back over the wire.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_msg_id = 7000

    async def send_to_topic(
        self, thread_id: int, text: str, **kwargs: Any,
    ) -> int:
        msg_id = self._next_msg_id
        self._next_msg_id += 1
        self.calls.append(
            {"topic_id": thread_id, "text": text, "kwargs": kwargs},
        )
        return msg_id


class _FakeRecord:
    def __init__(self, eng_id: str, *, topic_id: int | None) -> None:
        self.id = eng_id
        self.topic_id = topic_id


class _FakeRegistry:
    """Minimal stand-in for ``EngagementRegistry``.

    Pre-seeds one record (``eng-1`` → topic 42). Tests that need other
    shapes (missing record / record with no topic_id) override via
    ``set_record``.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, _FakeRecord] = {
            "eng-1": _FakeRecord("eng-1", topic_id=42),
        }

    def set_record(self, eng_id: str, rec: _FakeRecord | None) -> None:
        if rec is None:
            self._by_id.pop(eng_id, None)
        else:
            self._by_id[eng_id] = rec

    def get(self, eng_id: str) -> _FakeRecord | None:
        return self._by_id.get(eng_id)


# ---------------------------------------------------------------------------
# App factory fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def app_factory():
    from channels.channel_handlers import _make_channel_handlers

    def make(channel=None, registry=None):
        ch = channel or _FakeChannel()
        reg = registry or _FakeRegistry()
        handlers = _make_channel_handlers(
            telegram_channel=ch, engagement_registry=reg,
        )
        app = web.Application()
        for path, h in handlers.items():
            app.router.add_post(path, h)
        return app, ch, reg

    return make


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_send_to_topic_routes_by_engagement_id(app_factory) -> None:
    """Handler resolves engagement_id → topic_id via the registry and
    forwards the text. Response carries the channel's returned message_id."""
    app, ch, _reg = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/send_to_topic",
            json={"engagement_id": "eng-1", "text": "hello operator"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": True, "message_id": 7000}

    assert len(ch.calls) == 1
    assert ch.calls[0]["topic_id"] == 42
    assert ch.calls[0]["text"] == "hello operator"


async def test_send_to_topic_unknown_engagement_returns_error(
    app_factory,
) -> None:
    """Missing engagement record short-circuits with ``unknown_engagement``
    and never touches the telegram channel."""
    reg = _FakeRegistry()
    reg.set_record("eng-1", None)  # so registry.get("missing") returns None
    app, ch, _reg = app_factory(registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/send_to_topic",
            json={"engagement_id": "missing", "text": "hi"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": False, "error": "unknown_engagement"}

    assert ch.calls == []


async def test_send_to_topic_missing_topic_id_returns_error(
    app_factory,
) -> None:
    """Record exists but has no bound topic_id → ``no_topic_bound``,
    no telegram call."""
    reg = _FakeRegistry()
    reg.set_record("eng-1", _FakeRecord("eng-1", topic_id=None))
    app, ch, _reg = app_factory(registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/send_to_topic",
            json={"engagement_id": "eng-1", "text": "hi"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": False, "error": "no_topic_bound"}

    assert ch.calls == []
