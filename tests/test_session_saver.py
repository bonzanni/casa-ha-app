# tests/test_session_saver.py
"""Per-channel freshness windows (spec §3.3): voice short, telegram long."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from session_saver import freshness_window, reset_channel, save_session, transcript_to_items

pytestmark = [pytest.mark.unit]


def test_voice_is_short():
    assert freshness_window("voice") == timedelta(minutes=30)


def test_telegram_is_long():
    assert freshness_window("telegram") == timedelta(hours=12)


def test_unknown_channel_falls_back_to_telegram_default():
    assert freshness_window("something-else") == timedelta(hours=12)


def test_env_override(monkeypatch):
    monkeypatch.setenv("FRESHNESS_VOICE_MINUTES", "10")
    assert freshness_window("voice") == timedelta(minutes=10)


class _Msg:
    def __init__(self, type_, message):
        self.type = type_
        self.message = message


def test_transcript_to_items_builds_verified_shape():
    # SessionMessage.message is Any — handle both content-block and string forms.
    msgs = [
        _Msg("user", {"role": "user", "content": "What temp do I like?"}),
        _Msg("assistant", {"role": "assistant", "content": [{"type": "text", "text": "20C."}]}),
    ]
    items = transcript_to_items(msgs, sdk_session_id="sid-9", write_scope="house", user_peer="nicola")
    assert [i["content"] for i in items] == ["What temp do I like?", "20C."]
    assert items[0]["document_id"] == "sid-9:0" and items[1]["document_id"] == "sid-9:1"
    assert all(i["tags"] == ["house"] for i in items)
    assert items[0]["metadata"]["speaker"] == "nicola"
    assert items[1]["metadata"]["speaker"] == "assistant"


def test_transcript_to_items_skips_empty_and_toolonly():
    msgs = [_Msg("assistant", {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]})]
    assert transcript_to_items(msgs, sdk_session_id="s", write_scope="house", user_peer="nicola") == []


async def test_save_session_retains_and_finishes(tmp_path):
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("voice-r1", "assistant", "sid-9")
    await reg.record_write_scope("voice-r1", "house")
    sem = AsyncMock()  # SemanticMemory
    msgs = [type("M", (), {"type": "user", "message": {"role": "user", "content": "hi"}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        ok = await save_session(
            "voice-r1", reg, sem, role="assistant", directory="/addon_configs/casa-agent/agent-home/assistant",
            user_peer="voice_speaker",
        )
    assert ok is True
    sem.retain.assert_awaited_once()
    bank, items = sem.retain.await_args.args[0], sem.retain.await_args.kwargs.get("items") or sem.retain.await_args.args[1]
    assert bank == "casa-assistant"
    assert items[0]["content"] == "hi"
    assert reg.get("voice-r1") is None        # finished → entry removed


async def test_save_session_releases_claim_on_failure(tmp_path):
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("voice-r1", "assistant", "sid-9")
    await reg.record_write_scope("voice-r1", "house")
    sem = AsyncMock()
    sem.retain.side_effect = RuntimeError("hindsight down")
    msgs = [type("M", (), {"type": "user", "message": {"content": "hi"}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        ok = await save_session("voice-r1", reg, sem, role="assistant", directory="/d", user_peer="nicola")
    assert ok is False
    assert reg.get("voice-r1") is not None     # kept for retry
    assert not reg.get("voice-r1").get("consolidated_at")  # claim released


async def test_save_session_skips_when_already_claimed(tmp_path):
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("voice-r1", "assistant", "sid-9")
    await reg.try_begin_save("voice-r1")       # someone else claimed it
    sem = AsyncMock()
    ok = await save_session("voice-r1", reg, sem, role="assistant", directory="/d", user_peer="nicola")
    assert ok is False
    sem.retain.assert_not_awaited()


async def test_save_session_empty_transcript_still_finishes(tmp_path):
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("voice-r1", "assistant", "sid-9")
    await reg.record_write_scope("voice-r1", "house")
    sem = AsyncMock()
    # tool-only message → transcript_to_items returns [] → no retain, but still finishes
    msgs = [type("M", (), {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        ok = await save_session("voice-r1", reg, sem, role="assistant", directory="/d", user_peer="p")
    assert ok is True
    sem.retain.assert_not_awaited()
    assert reg.get("voice-r1") is None


async def test_save_session_no_write_scope_releases_claim(tmp_path):
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("voice-r1", "assistant", "sid-9")  # write_scope never recorded
    sem = AsyncMock()
    ok = await save_session("voice-r1", reg, sem, role="assistant", directory="/d", user_peer="p")
    assert ok is False
    sem.retain.assert_not_awaited()
    assert not reg.get("voice-r1").get("consolidated_at")  # claim released, entry kept


async def test_reset_channel_saves_then_clears(tmp_path):
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-42", "assistant", "sid-9")
    await reg.record_write_scope("telegram-42", "house")
    sem = AsyncMock()
    msgs = [type("M", (), {"type": "user", "message": {"content": "remember X"}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        await reset_channel("telegram-42", reg, sem, channel="telegram")
    sem.retain.assert_awaited_once()        # saved before clearing
    assert reg.get("telegram-42") is None   # pointer cleared → next turn starts fresh


async def test_reset_channel_no_entry_is_noop(tmp_path):
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    sem = AsyncMock()
    await reset_channel("telegram-99", reg, sem, channel="telegram")
    sem.retain.assert_not_awaited()         # nothing to save
    assert reg.get("telegram-99") is None
