# tests/test_session_saver_tiers.py
"""Save path: per-item true-tier tags, shared bank, voice is recall-only."""
import pytest

import session_saver

pytestmark = [pytest.mark.unit]


class _Msg:
    def __init__(self, mtype, text):
        self.type = mtype
        self.message = {"role": mtype, "content": text}


async def test_transcript_items_tagged_per_item(monkeypatch):
    async def fake_classify(content: str) -> str:
        return "private" if "salary" in content else "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    msgs = [_Msg("user", "my salary is 5000"), _Msg("assistant", "bin day is Tuesday")]
    items = await session_saver.transcript_to_items(
        msgs, sdk_session_id="s1", user_peer="nicola",
    )
    assert [i["tags"] for i in items] == [["private"], ["public"]]
    assert [i["document_id"] for i in items] == ["s1:0", "s1:1"]


async def test_save_session_skips_voice():
    retained = []

    class _Sem:
        async def retain(self, bank, items, *, async_=True):
            retained.append(bank)

    class _Reg:
        async def try_begin_save(self, k): return True
        def get(self, k): return {"sdk_session_id": "s1"}
        async def clear_save_claim(self, k, sid=None): pass
        async def finish_save(self, k, sid=None): pass

    ok = await session_saver.save_session(
        "voice-abc", _Reg(), _Sem(), role="butler",
        directory="/tmp", user_peer="voice_speaker", channel="voice",
    )
    assert ok is False
    assert retained == []  # voice persists nothing (recall-only)


async def test_save_session_retains_telegram_to_shared_bank(monkeypatch):
    async def fake_classify(content: str) -> str:
        return "friends"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    monkeypatch.setattr(
        session_saver, "get_session_messages",
        lambda sid, directory: [_Msg("user", "kids like pizza")],
    )
    retained = {}

    class _Sem:
        async def retain(self, bank, items, *, async_=True):
            retained["bank"] = bank
            retained["tags"] = [i["tags"] for i in items]

    class _Reg:
        async def try_begin_save(self, k): return True
        def get(self, k): return {"sdk_session_id": "s1"}
        async def finish_save(self, k, sid=None): pass
        async def clear_save_claim(self, k, sid=None): pass

    ok = await session_saver.save_session(
        "telegram-1", _Reg(), _Sem(), role="assistant",
        directory="/tmp", user_peer="nicola", channel="telegram",
    )
    assert ok is True
    assert retained["bank"] == "casa"
    assert retained["tags"] == [["friends"]]


async def test_transcript_classification_is_concurrent(monkeypatch):
    """M29: classification must overlap (bounded gather), not run one-at-a-time,
    while preserving order and idempotent document_ids."""
    import asyncio
    in_flight = 0
    peak = 0

    async def fake_classify(content: str) -> str:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return "public"

    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    msgs = [_Msg("user", f"fact {i}") for i in range(8)]
    items = await session_saver.transcript_to_items(
        msgs, sdk_session_id="s1", user_peer="nicola",
    )
    assert len(items) == 8
    # Serial gives peak == 1; bounded-parallel gives peak in [2, 4].
    assert peak >= 2, f"classification ran serially (peak in-flight = {peak})"
    assert peak <= session_saver._CLASSIFY_CONCURRENCY, "semaphore bound exceeded"
    assert [i["document_id"] for i in items] == [f"s1:{i}" for i in range(8)]
