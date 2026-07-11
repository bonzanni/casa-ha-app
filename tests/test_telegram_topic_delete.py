"""Tests for the Telegram side of topic retention & cleanup (v0.65.0).

Covers the 2026-07-10 topic-retention-cleanup design's telegram.py surface:

- [AR-3]/[AR-9] ``TelegramChannel.delete_topic``: calls
  ``delete_forum_topic`` against the configured engagement supergroup,
  raises RuntimeError when the supergroup is unconfigured, refuses the
  General/invalid thread ids (None, 0, 1) with a ValueError, and —
  deliberately unlike ``close_topic`` — PROPAGATES Telegram exceptions
  so the ledger sweep can classify the real error.
- [AR-1] the two direct-``mark_error`` terminal paths in
  ``handle_update`` (``resume_failed``, ``orphan_no_session``): each
  best-effort closes the topic and appends a ledger entry after
  mark_error; a close failure must not skip the append, and a ledger
  failure must not break the resume-failure handling itself.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

import telegram.error as tg_err

import topic_ledger


# ---------------------------------------------------------------------------
# telegram.error stubs — conftest installs a minimal telegram.error stub
# (TelegramError / NetworkError / TimedOut only). The propagation tests also
# need BadRequest; attach the missing classes here mirroring the REAL
# python-telegram-bot hierarchy (BadRequest and TimedOut subclass
# NetworkError in PTB 22.7). hasattr-guarded: no-ops against the real
# library or if another test file (test_topic_ledger.py) attached them first.
# ---------------------------------------------------------------------------


def _ensure_stub_error_classes() -> None:
    if not hasattr(tg_err, "BadRequest"):
        class BadRequest(tg_err.NetworkError):
            pass

        tg_err.BadRequest = BadRequest
    if not hasattr(tg_err, "Forbidden"):
        class Forbidden(tg_err.TelegramError):
            pass

        tg_err.Forbidden = Forbidden
    if not hasattr(tg_err, "RetryAfter"):
        class RetryAfter(tg_err.TelegramError):
            def __init__(self, retry_after):
                super().__init__(
                    f"Flood control exceeded. Retry in {retry_after} seconds"
                )
                self.retry_after = retry_after

        tg_err.RetryAfter = RetryAfter


_ensure_stub_error_classes()


SUPERGROUP = -1001
DM_CHAT = 100


@pytest.fixture(autouse=True)
def _fresh_module_state(monkeypatch, tmp_path):
    """Isolate every test: a fresh module lock (asyncio primitives bind the
    first loop they're used on; pytest-asyncio gives each test its own loop)
    and a tmp default LEDGER_PATH so nothing can ever touch /data."""
    monkeypatch.setattr(topic_ledger, "_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        topic_ledger, "LEDGER_PATH", str(tmp_path / "topic-ledger.json")
    )


def _mk_channel(fake_telegram_bot, supergroup_id=SUPERGROUP):
    from channels.telegram import TelegramChannel

    return TelegramChannel(
        bot=fake_telegram_bot, chat_id=DM_CHAT,
        engagement_supergroup_id=supergroup_id,
    )


def _mk_update(*, chat_id, text, thread_id=None, user_id=77):
    u = MagicMock()
    u.message = MagicMock()
    u.message.chat = MagicMock()
    u.message.chat.id = chat_id
    u.message.text = text
    u.message.message_thread_id = thread_id
    u.message.from_user = MagicMock(id=user_id)
    u.message.message_id = 999
    return u


def _channel_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "channels.telegram" and r.levelno >= logging.WARNING
    ]


# ---------------------------------------------------------------------------
# delete_topic [AR-3]/[AR-9]
# ---------------------------------------------------------------------------


class TestDeleteTopic:
    async def test_calls_delete_forum_topic_with_supergroup_chat_id(
        self, fake_telegram_bot,
    ):
        ch = _mk_channel(fake_telegram_bot)
        fake_telegram_bot.delete_forum_topic = AsyncMock(return_value=True)

        await ch.delete_topic(613)

        fake_telegram_bot.delete_forum_topic.assert_awaited_once_with(
            chat_id=SUPERGROUP, message_thread_id=613,
        )

    async def test_raises_runtime_error_when_supergroup_unconfigured(
        self, fake_telegram_bot,
    ):
        ch = _mk_channel(fake_telegram_bot, supergroup_id=None)
        fake_telegram_bot.delete_forum_topic = AsyncMock()

        with pytest.raises(RuntimeError, match="engagement supergroup"):
            await ch.delete_topic(613)
        fake_telegram_bot.delete_forum_topic.assert_not_awaited()

    @pytest.mark.parametrize("thread_id", [None, 0, 1])
    async def test_refuses_general_and_invalid_thread_ids(
        self, fake_telegram_bot, thread_id,
    ):
        """[AR-9] 1 is the supergroup's General topic; None/0 mean 'no
        topic'. A deletion attempt must be refused before any API call."""
        ch = _mk_channel(fake_telegram_bot)
        fake_telegram_bot.delete_forum_topic = AsyncMock()

        with pytest.raises(ValueError):
            await ch.delete_topic(thread_id)
        fake_telegram_bot.delete_forum_topic.assert_not_awaited()

    async def test_propagates_telegram_errors_unlike_close_topic(
        self, fake_telegram_bot,
    ):
        """[AR-3] The outcome-classification contract depends on callers
        seeing the REAL exception — a close_topic-style swallow-everything
        except would make every failure look like success and silently
        drop ledger entries even under permission denial."""
        ch = _mk_channel(fake_telegram_bot)
        boom = tg_err.BadRequest("not enough rights")
        fake_telegram_bot.delete_forum_topic = AsyncMock(side_effect=boom)

        with pytest.raises(tg_err.BadRequest) as excinfo:
            await ch.delete_topic(613)
        assert excinfo.value is boom


# ---------------------------------------------------------------------------
# [AR-1] direct-mark_error terminal paths in handle_update
# ---------------------------------------------------------------------------


def _wire_error_path(ch, registry, *, resume_side_effect=None):
    """Wire the collaborators handle_update's resume block needs: a dead
    in_casa driver plus the registry. ``resume_side_effect`` scripts
    ``driver.resume`` (None = never reached, the orphan path)."""
    ch._engagement_registry = registry
    ch._driver_send_user_turn = AsyncMock()
    driver = MagicMock()
    driver.is_alive = MagicMock(return_value=False)
    driver.resume = AsyncMock(side_effect=resume_side_effect)
    ch._engagement_driver = driver
    return driver


async def _drive_resume_failed(ch, rec, registry):
    """Drive the ``resume_failed`` path: a persisted sdk_session_id, a dead
    driver whose resume raises, and two user turns (mark_error fires on the
    second failure — fail_count >= 2)."""
    await registry.persist_session_id(rec.id, "sess-xyz")
    await registry.mark_idle(rec.id)
    for text in ("turn1", "turn2"):
        await ch.handle_update(
            _mk_update(chat_id=SUPERGROUP, text=text, thread_id=rec.topic_id)
        )


async def _drive_orphan_no_session(ch, rec):
    """Drive the ``orphan_no_session`` path: a dead driver and no
    sdk_session_id to resume with (the fixture record's default)."""
    assert rec.sdk_session_id is None  # precondition for the orphan branch
    await ch.handle_update(
        _mk_update(chat_id=SUPERGROUP, text="hello?", thread_id=rec.topic_id)
    )


class TestResumeFailedTerminalCleanup:
    async def test_marks_error_closes_topic_and_appends_ledger_entry(
        self, fake_telegram_bot, engagement_fixture,
    ):
        ch = _mk_channel(fake_telegram_bot)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record  # topic_id=555
        _wire_error_path(ch, registry, resume_side_effect=RuntimeError("rotated"))
        fake_telegram_bot.close_forum_topic = AsyncMock(return_value=True)

        await _drive_resume_failed(ch, rec, registry)

        assert rec.status == "error"
        assert rec.origin["error_kind"] == "resume_failed"
        fake_telegram_bot.close_forum_topic.assert_awaited_once_with(
            chat_id=SUPERGROUP, message_thread_id=rec.topic_id,
        )
        (entry,) = await topic_ledger.load()
        assert entry["engagement_id"] == rec.id
        assert entry["chat_id"] == SUPERGROUP
        assert entry["topic_id"] == rec.topic_id
        assert entry["outcome"] == "error"

    async def test_ledger_entry_appended_even_when_close_topic_raises(
        self, fake_telegram_bot, engagement_fixture, caplog,
    ):
        ch = _mk_channel(fake_telegram_bot)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        _wire_error_path(ch, registry, resume_side_effect=RuntimeError("rotated"))
        ch.close_topic = AsyncMock(side_effect=RuntimeError("telegram down"))

        with caplog.at_level(logging.WARNING):
            await _drive_resume_failed(ch, rec, registry)

        assert rec.status == "error"
        (entry,) = await topic_ledger.load()
        assert entry["engagement_id"] == rec.id
        assert entry["outcome"] == "error"
        assert any("close_topic failed" in m for m in _channel_warnings(caplog))
        # The user notice after the cleanup still went out — the failure
        # never broke the resume-failure handling itself.
        msgs = fake_telegram_bot._supergroups[SUPERGROUP].messages_by_thread[
            rec.topic_id
        ]
        assert any("Could not resume" in m for m in msgs)

    async def test_user_notice_sent_before_topic_close(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """Posting into a just-closed topic works only while the bot keeps
        can_manage_topics — the funnel's deliberate send-then-close order
        must be mirrored here: notice out first, cleanup after."""
        ch = _mk_channel(fake_telegram_bot)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        _wire_error_path(ch, registry, resume_side_effect=RuntimeError("rotated"))

        order: list[str] = []
        real_send = fake_telegram_bot.send_message

        async def send_spy(*args, **kwargs):
            order.append("notice")
            return await real_send(*args, **kwargs)

        async def close_spy(*args, **kwargs):
            order.append("close")
            return True

        fake_telegram_bot.send_message = send_spy
        fake_telegram_bot.close_forum_topic = close_spy

        await _drive_resume_failed(ch, rec, registry)

        # Turn 1 notice, turn 2 notice, THEN the terminal cleanup's close.
        assert order == ["notice", "notice", "close"], (
            "the user notice must go out before the topic is closed"
        )

    async def test_topic_title_marked_failed(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """Error-path topics must carry the terminal title mark, like every
        finalize-funnel terminal topic."""
        ch = _mk_channel(fake_telegram_bot)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        _wire_error_path(ch, registry, resume_side_effect=RuntimeError("rotated"))
        ch.update_topic_state = AsyncMock()
        fake_telegram_bot.close_forum_topic = AsyncMock(return_value=True)

        await _drive_resume_failed(ch, rec, registry)

        ch.update_topic_state.assert_awaited_once_with(
            engagement_id=rec.id, new_state="failed",
        )
        fake_telegram_bot.close_forum_topic.assert_awaited_once()

    async def test_cleanup_completes_when_update_topic_state_raises(
        self, fake_telegram_bot, engagement_fixture, caplog,
    ):
        """The title mark is best-effort — its failure must not skip the
        close or the ledger append."""
        ch = _mk_channel(fake_telegram_bot)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        _wire_error_path(ch, registry, resume_side_effect=RuntimeError("rotated"))
        ch.update_topic_state = AsyncMock(side_effect=RuntimeError("edit down"))
        fake_telegram_bot.close_forum_topic = AsyncMock(return_value=True)

        with caplog.at_level(logging.WARNING):
            await _drive_resume_failed(ch, rec, registry)

        assert rec.status == "error"
        fake_telegram_bot.close_forum_topic.assert_awaited_once()
        (entry,) = await topic_ledger.load()
        assert entry["engagement_id"] == rec.id
        assert any(
            "update_topic_state failed" in m for m in _channel_warnings(caplog)
        )

    async def test_resume_failure_handling_completes_when_append_raises(
        self, fake_telegram_bot, engagement_fixture, caplog, monkeypatch,
    ):
        """topic_ledger.append RAISES on I/O failure by design — the
        wrap-and-continue at the call site is what keeps mark_error's
        surrounding flow alive."""
        ch = _mk_channel(fake_telegram_bot)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        _wire_error_path(ch, registry, resume_side_effect=RuntimeError("rotated"))
        fake_telegram_bot.close_forum_topic = AsyncMock(return_value=True)
        monkeypatch.setattr(
            topic_ledger, "append", AsyncMock(side_effect=OSError("disk full")),
        )

        with caplog.at_level(logging.WARNING):
            await _drive_resume_failed(ch, rec, registry)

        assert rec.status == "error"
        assert rec.origin["error_kind"] == "resume_failed"
        # Close was still attempted (append comes second, close first).
        fake_telegram_bot.close_forum_topic.assert_awaited_once()
        assert any(
            "topic_ledger.append failed" in m for m in _channel_warnings(caplog)
        )
        msgs = fake_telegram_bot._supergroups[SUPERGROUP].messages_by_thread[
            rec.topic_id
        ]
        assert any("Could not resume" in m for m in msgs)


class TestOrphanNoSessionTerminalCleanup:
    async def test_marks_error_closes_topic_and_appends_ledger_entry(
        self, fake_telegram_bot, engagement_fixture,
    ):
        ch = _mk_channel(fake_telegram_bot)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        driver = _wire_error_path(ch, registry)
        fake_telegram_bot.close_forum_topic = AsyncMock(return_value=True)

        await _drive_orphan_no_session(ch, rec)

        assert rec.status == "error"
        assert rec.origin["error_kind"] == "orphan_no_session"
        driver.resume.assert_not_awaited()
        fake_telegram_bot.close_forum_topic.assert_awaited_once_with(
            chat_id=SUPERGROUP, message_thread_id=rec.topic_id,
        )
        (entry,) = await topic_ledger.load()
        assert entry["engagement_id"] == rec.id
        assert entry["chat_id"] == SUPERGROUP
        assert entry["topic_id"] == rec.topic_id
        assert entry["outcome"] == "error"

    async def test_ledger_entry_appended_even_when_close_topic_raises(
        self, fake_telegram_bot, engagement_fixture, caplog,
    ):
        ch = _mk_channel(fake_telegram_bot)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        _wire_error_path(ch, registry)
        ch.close_topic = AsyncMock(side_effect=RuntimeError("telegram down"))

        with caplog.at_level(logging.WARNING):
            await _drive_orphan_no_session(ch, rec)

        assert rec.status == "error"
        (entry,) = await topic_ledger.load()
        assert entry["engagement_id"] == rec.id
        assert entry["outcome"] == "error"
        assert any("close_topic failed" in m for m in _channel_warnings(caplog))
        msgs = fake_telegram_bot._supergroups[SUPERGROUP].messages_by_thread[
            rec.topic_id
        ]
        assert any("can't be resumed" in m for m in msgs)

    async def test_user_notice_sent_before_topic_close(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """Same send-then-close mirror as the resume_failed path."""
        ch = _mk_channel(fake_telegram_bot)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        _wire_error_path(ch, registry)

        order: list[str] = []
        real_send = fake_telegram_bot.send_message

        async def send_spy(*args, **kwargs):
            order.append("notice")
            return await real_send(*args, **kwargs)

        async def close_spy(*args, **kwargs):
            order.append("close")
            return True

        fake_telegram_bot.send_message = send_spy
        fake_telegram_bot.close_forum_topic = close_spy

        await _drive_orphan_no_session(ch, rec)

        assert order == ["notice", "close"], (
            "the user notice must go out before the topic is closed"
        )

    async def test_topic_title_marked_failed(
        self, fake_telegram_bot, engagement_fixture,
    ):
        ch = _mk_channel(fake_telegram_bot)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        _wire_error_path(ch, registry)
        ch.update_topic_state = AsyncMock()
        fake_telegram_bot.close_forum_topic = AsyncMock(return_value=True)

        await _drive_orphan_no_session(ch, rec)

        ch.update_topic_state.assert_awaited_once_with(
            engagement_id=rec.id, new_state="failed",
        )
        fake_telegram_bot.close_forum_topic.assert_awaited_once()

    async def test_orphan_handling_completes_when_append_raises(
        self, fake_telegram_bot, engagement_fixture, caplog, monkeypatch,
    ):
        ch = _mk_channel(fake_telegram_bot)
        registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        _wire_error_path(ch, registry)
        fake_telegram_bot.close_forum_topic = AsyncMock(return_value=True)
        monkeypatch.setattr(
            topic_ledger, "append", AsyncMock(side_effect=OSError("disk full")),
        )

        with caplog.at_level(logging.WARNING):
            await _drive_orphan_no_session(ch, rec)

        assert rec.status == "error"
        assert rec.origin["error_kind"] == "orphan_no_session"
        fake_telegram_bot.close_forum_topic.assert_awaited_once()
        assert any(
            "topic_ledger.append failed" in m for m in _channel_warnings(caplog)
        )
        msgs = fake_telegram_bot._supergroups[SUPERGROUP].messages_by_thread[
            rec.topic_id
        ]
        assert any("can't be resumed" in m for m in msgs)
