# tests/test_session_saver.py
"""Per-channel freshness windows (spec §3.3): voice short, telegram long."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

import session_saver
from hindsight_ids import agent_document_id, content_document_id
from session_saver import freshness_window, reset_channel, save_session, transcript_to_items
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV

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


async def test_transcript_to_items_builds_verified_shape(monkeypatch):
    # SessionMessage.message is Any — handle both content-block and string forms.
    # classify_tier is monkeypatched to a deterministic fake (avoids SDK I/O).
    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    msgs = [
        _Msg("user", {"role": "user", "content": "What temp do I like?"}),
        _Msg("assistant", {"role": "assistant", "content": [{"type": "text", "text": "20C."}]}),
    ]
    items = await transcript_to_items(
        msgs, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )
    assert [i["content"] for i in items] == ["What temp do I like?", "20C."]
    # Task 10: content-derived document_id, keyed by KIND — user turn on its
    # user_peer, assistant turn on its persona identity.
    assert items[0]["document_id"] == content_document_id("tester", "What temp do I like?")
    assert items[1]["document_id"] == agent_document_id(STUB_SPEAKER_PROV, "20C.")
    # Exactly one tier tag (first) + one reserved provenance tag per item.
    assert all(i["tags"][0] == "public" for i in items)
    assert all(sum(1 for t in i["tags"] if t.startswith("casa-source-")) == 1 for i in items)
    # Provenance survives into metadata for reconstruction.
    assert "casa_source_v1" in items[0]["metadata"]
    assert "casa_source_v1" in items[1]["metadata"]


async def test_transcript_to_items_skips_empty_and_toolonly(monkeypatch):
    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    msgs = [_Msg("assistant", {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]})]
    result = await transcript_to_items(
        msgs, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )
    assert result == []


async def test_save_session_retains_and_finishes(tmp_path, monkeypatch):
    async def fake_classify(content: str) -> str:
        return "friends"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    sem = AsyncMock()  # SemanticMemory
    msgs = [type("M", (), {"type": "user", "message": {"role": "user", "content": "hi"}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        ok = await save_session(
            "telegram-r1", reg, sem,
            directory="/addon_configs/casa-agent/agent-home/assistant",
            channel="telegram",
        )
    assert ok is True
    sem.retain.assert_awaited_once()
    bank, items = sem.retain.await_args.args[0], sem.retain.await_args.kwargs.get("items") or sem.retain.await_args.args[1]
    assert bank == "casa"
    assert items[0]["content"] == "hi"
    assert items[0]["tags"][0] == "friends"       # tier tag first (+ provenance tag)
    assert reg.get("telegram-r1") is None        # finished → entry removed


async def test_save_session_releases_claim_on_failure(tmp_path, monkeypatch):
    async def fake_classify(content: str) -> str:
        return "private"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    sem = AsyncMock()
    sem.retain.side_effect = RuntimeError("hindsight down")
    msgs = [type("M", (), {"type": "user", "message": {"content": "hi"}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        ok = await save_session(
            "telegram-r1", reg, sem, directory="/d", channel="telegram",
        )
    assert ok is False
    assert reg.get("telegram-r1") is not None     # kept for retry
    assert not reg.get("telegram-r1").get("consolidated_at")  # claim released


async def test_save_session_skips_when_already_claimed(tmp_path):
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    await reg.try_begin_save("telegram-r1")       # someone else claimed it
    sem = AsyncMock()
    ok = await save_session(
        "telegram-r1", reg, sem, directory="/d", channel="telegram",
    )
    assert ok is False
    sem.retain.assert_not_awaited()


async def test_save_session_empty_transcript_still_finishes(tmp_path):
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    sem = AsyncMock()
    # tool-only message → transcript_to_items returns [] → no retain, but still finishes
    msgs = [type("M", (), {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        ok = await save_session(
            "telegram-r1", reg, sem, directory="/d", channel="telegram",
        )
    assert ok is True
    sem.retain.assert_not_awaited()
    assert reg.get("telegram-r1") is None


async def test_save_session_no_sid_releases_claim(tmp_path):
    """Entry with no sdk_session_id → claim is released and False returned."""
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    # Plant an entry directly (no sdk_session_id) to hit the sid-guard.
    reg._data["telegram-r1"] = {"agent": "assistant"}
    sem = AsyncMock()
    ok = await save_session(
        "telegram-r1", reg, sem, directory="/d", channel="telegram",
    )
    assert ok is False
    sem.retain.assert_not_awaited()
    assert not reg.get("telegram-r1").get("consolidated_at")  # claim released


async def test_save_session_voice_skips_entirely(tmp_path):
    """Voice channel → writes_to_bank returns False → skip before any claim."""
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("voice-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    sem = AsyncMock()
    ok = await save_session(
        "voice-r1", reg, sem, directory="/d", channel="voice",
    )
    assert ok is False
    sem.retain.assert_not_awaited()
    # Entry is still present (not claimed) — voice sessions can be reaped after they go cold
    assert reg.get("voice-r1") is not None


async def test_reset_channel_saves_then_clears(tmp_path, monkeypatch):
    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-42", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
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
