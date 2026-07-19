"""Store invariant guardrail (Release A, Layer 5).

Webhook-origin content must never reach the shared memory bank. Rather than
build origin-aware store-deny machinery, Release A relies on the pre-existing
channel write-trust gate (``_WRITABLE_CHANNELS == {"telegram"}``). These tests
PIN that invariant so a future change granting webhook/invoke write-trust
deliberately re-opens the store-deny question instead of silently leaking.
"""
from __future__ import annotations

import pytest

from channel_policy import writes_to_bank

pytestmark = [pytest.mark.unit]


class _RecordingSem:
    def __init__(self):
        self.retained = []

    async def retain(self, bank, items, *, async_=False):
        self.retained.append((bank, items))


def test_webhook_channel_is_not_writable():
    assert writes_to_bank("webhook") is False
    assert writes_to_bank("telegram") is True
    assert writes_to_bank("voice") is False


async def test_retain_cold_session_noops_for_webhook():
    from session_saver import retain_cold_session
    sem = _RecordingSem()
    await retain_cold_session(
        sid="s1", role="assistant", directory="/tmp/none",
        user_peer="u", channel="webhook", semantic_memory=sem,
    )
    assert sem.retained == []  # webhook → recall-only → nothing persisted


async def test_retain_delegated_noops_for_webhook_origin():
    from delegated_memory import retain_delegated
    sem = _RecordingSem()
    await retain_delegated(
        sem, origin_channel="webhook", doc_prefix="d",
        turns=[("user", "a secret"), ("assistant", "ok")],
    )
    assert sem.retained == []


async def test_save_session_returns_false_for_webhook():
    from session_saver import save_session
    sem = _RecordingSem()
    # writes_to_bank gate is the first check in save_session — it returns False
    # before touching the registry, so a minimal registry stub is unused.
    result = await save_session(
        "webhook-assistant-x", registry=object(), semantic_memory=sem,
        role="assistant", directory="/tmp/none", user_peer="u", channel="webhook",
    )
    assert result is False
    assert sem.retained == []
