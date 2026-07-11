"""AR-4: /new must flush (close) the warm client BEFORE save_session reads
the transcript from disk."""
from __future__ import annotations

import pytest

from session_registry import SessionRegistry

pytestmark = pytest.mark.asyncio


async def test_notify_reset_calls_listener_and_unsubscribes(tmp_path):
    reg = SessionRegistry(str(tmp_path / "sessions.json"))
    calls = []

    async def listener(key):
        calls.append(key)

    unsub = reg.add_reset_listener(listener)
    await reg.notify_reset("telegram-1")
    assert calls == ["telegram-1"]
    unsub()
    await reg.notify_reset("telegram-1")
    assert calls == ["telegram-1"]


async def test_notify_reset_survives_listener_error(tmp_path):
    reg = SessionRegistry(str(tmp_path / "sessions.json"))
    async def bad(key): raise RuntimeError("boom")
    seen = []
    async def good(key): seen.append(key)
    reg.add_reset_listener(bad)
    reg.add_reset_listener(good)
    await reg.notify_reset("k")            # must not raise
    assert seen == ["k"]


async def test_reset_channel_notifies_before_save(tmp_path, monkeypatch):
    import session_saver
    reg = SessionRegistry(str(tmp_path / "sessions.json"))
    await reg.register("telegram-1", "assistant", "sid-1")
    order = []

    async def listener(key): order.append(f"reset:{key}")
    reg.add_reset_listener(listener)

    async def fake_save(channel_key, registry, memory, **kw):
        order.append("save")
    monkeypatch.setattr(session_saver, "save_session", fake_save)

    await session_saver.reset_channel(
        "telegram-1", reg, object(), channel="telegram",
    )
    assert order == ["reset:telegram-1", "save"]
    assert reg.get("telegram-1") is None
