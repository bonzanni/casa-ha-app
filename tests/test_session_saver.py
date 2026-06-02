# tests/test_session_saver.py
"""Per-channel freshness windows (spec §3.3): voice short, telegram long."""
from __future__ import annotations

from datetime import timedelta

import pytest

from session_saver import freshness_window

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


from session_saver import transcript_to_items


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
