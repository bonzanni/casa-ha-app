"""Tests for TelegramChannel inline-callback dispatch (v0.37.0 Phase 2).

Covers Task 20 (CallbackQueryHandler routes permission verdicts via
``/internal/channel/permission_verdict``) and the supporting ``_internal_post``
helper. The handler:

1. ``await update.callback_query.answer()`` — clears the Telegram client-side
   spinner regardless of outcome (Telegram requires it).
2. Parses ``callback_data`` as ``perm:<verdict>:<request_id>``.
3. Resolves topic_id → engagement_id via the registry.
4. POSTs the verdict to casa-main's ``/internal/channel/permission_verdict``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _mk_callback_update(*, data, thread_id, chat_id, query_id="cq1", user_id=999):
    cq = SimpleNamespace(
        id=query_id,
        data=data,
        message=SimpleNamespace(
            message_thread_id=thread_id,
            chat=SimpleNamespace(id=chat_id),
        ),
        answer=AsyncMock(return_value=None),
        from_user=SimpleNamespace(id=user_id),
    )
    return SimpleNamespace(callback_query=cq)


class TestInlineCallbackPermissionVerdict:
    async def test_perm_allow_dispatches_verdict(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel
        posted: list[tuple[str, dict]] = []

        async def fake_post(path, payload):
            posted.append((path, dict(payload)))
            return {"ok": True}

        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        ch._internal_post = fake_post  # type: ignore[method-assign]
        rec = engagement_fixture.active_record  # topic_id=555

        update = _mk_callback_update(
            data="perm:allow:rid-001", thread_id=rec.topic_id, chat_id=-1001,
            user_id=999,
        )
        await ch._on_inline_callback(update, context=None)

        # 1. Telegram spinner cleared.
        update.callback_query.answer.assert_awaited_once()

        # 2. Verdict POSTed with engagement_id resolved from topic_id.
        assert posted == [(
            "/internal/channel/permission_verdict",
            {"request_id": "rid-001", "verdict": "allow",
             "engagement_id": rec.id, "operator_id": 999},
        )]

    async def test_perm_deny_dispatches_verdict(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel
        posted: list[tuple[str, dict]] = []

        async def fake_post(path, payload):
            posted.append((path, dict(payload)))
            return {"ok": True}

        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        ch._internal_post = fake_post  # type: ignore[method-assign]
        rec = engagement_fixture.active_record

        update = _mk_callback_update(
            data="perm:deny:rid-XYZ", thread_id=rec.topic_id, chat_id=-1001,
            user_id=42,
        )
        await ch._on_inline_callback(update, context=None)

        assert posted == [(
            "/internal/channel/permission_verdict",
            {"request_id": "rid-XYZ", "verdict": "deny",
             "engagement_id": rec.id, "operator_id": 42},
        )]

    async def test_unknown_topic_logs_and_drops(
        self, fake_telegram_bot, engagement_fixture, caplog,
    ):
        """Defensive: a callback from a topic with no engagement record is
        acknowledged (spinner clears) but produces no internal POST."""
        from channels.telegram import TelegramChannel
        posted: list[tuple[str, dict]] = []

        async def fake_post(path, payload):
            posted.append((path, dict(payload)))
            return {"ok": True}

        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        ch._internal_post = fake_post  # type: ignore[method-assign]

        update = _mk_callback_update(
            data="perm:allow:rid-1", thread_id=99999, chat_id=-1001,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once()
        assert posted == []

    async def test_malformed_callback_data_dropped(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel
        posted: list[tuple[str, dict]] = []

        async def fake_post(path, payload):
            posted.append((path, dict(payload)))
            return {"ok": True}

        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        ch._internal_post = fake_post  # type: ignore[method-assign]
        rec = engagement_fixture.active_record

        for data in ("not-perm:x:y", "perm:nope:rid", "perm:allow"):
            update = _mk_callback_update(
                data=data, thread_id=rec.topic_id, chat_id=-1001,
            )
            await ch._on_inline_callback(update, context=None)
            update.callback_query.answer.assert_awaited_once()

        assert posted == []  # No verdict dispatched for any malformed data.

    async def test_internal_post_failure_does_not_propagate(
        self, fake_telegram_bot, engagement_fixture, caplog,
    ):
        """If casa-main is unreachable the handler must NOT bubble the error
        back into PTB's update loop — log + drop."""
        from channels.telegram import TelegramChannel

        async def exploding_post(path, payload):
            raise RuntimeError("socket down")

        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        ch._internal_post = exploding_post  # type: ignore[method-assign]
        rec = engagement_fixture.active_record

        update = _mk_callback_update(
            data="perm:allow:rid-1", thread_id=rec.topic_id, chat_id=-1001,
        )
        await ch._on_inline_callback(update, context=None)
        update.callback_query.answer.assert_awaited_once()


class TestUpdateTopicState:
    """E-12 (v0.37.0) Task 23: TelegramChannel.update_topic_state."""

    async def test_update_state_edits_topic_title(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel
        # The engagement_fixture creates only a registry record with
        # topic_id=555. Pre-create the forum topic on the fake bot so
        # update_topic_state's edit_forum_topic finds it.
        await fake_telegram_bot.create_forum_topic(
            chat_id=-1001, name="🟢·🤖 t",
        )
        # The fake's auto-assigned thread id starts at 1001; manually align
        # the fixture's topic_id to that value.
        rec = engagement_fixture.active_record
        rec.topic_id = 1001

        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry

        await ch.update_topic_state(
            engagement_id=rec.id, new_state="awaiting",
        )

        sg = fake_telegram_bot._supergroups[-1001]
        topic = sg.topics[1001]
        assert topic.name.startswith("🟡·"), f"got {topic.name!r}"

    async def test_update_state_is_noop_when_state_unchanged(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        # Seed the registry with current_state_emoji=🟡 already.
        await engagement_fixture.registry.set_channel_state(
            rec.id, current_state_emoji="🟡",
        )

        # Sanity: edit_forum_topic shouldn't be called.
        from unittest.mock import AsyncMock
        ef = AsyncMock()
        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", ef)

        await ch.update_topic_state(
            engagement_id=rec.id, new_state="awaiting",
        )
        ef.assert_not_awaited()

    async def test_update_state_unknown_state_drops_silently(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record

        from unittest.mock import AsyncMock
        ef = AsyncMock()
        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", ef)

        await ch.update_topic_state(
            engagement_id=rec.id, new_state="not-a-real-state",
        )
        ef.assert_not_awaited()

    async def test_update_state_unknown_engagement_drops_silently(
        self, fake_telegram_bot, engagement_fixture, monkeypatch,
    ):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry

        from unittest.mock import AsyncMock
        ef = AsyncMock()
        monkeypatch.setattr(fake_telegram_bot, "edit_forum_topic", ef)

        await ch.update_topic_state(
            engagement_id="does-not-exist", new_state="awaiting",
        )
        ef.assert_not_awaited()

    async def test_update_state_persists_current_state_emoji(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel
        await fake_telegram_bot.create_forum_topic(
            chat_id=-1001, name="🟢·🤖 t",
        )
        rec = engagement_fixture.active_record
        rec.topic_id = 1001

        ch = TelegramChannel(
            bot=fake_telegram_bot, chat_id=100, engagement_supergroup_id=-1001,
        )
        ch._engagement_registry = engagement_fixture.registry
        assert rec.current_state_emoji is None  # baseline

        await ch.update_topic_state(
            engagement_id=rec.id, new_state="awaiting",
        )
        assert rec.current_state_emoji == "🟡"
