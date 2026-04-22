"""Tests for the engagement observer — classifier + rate limit + silent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


class TestClassifier:
    @pytest.mark.parametrize("event,expected", [
        ("started", "peek"),
        ("user_turn", "silent"),
        ("tool_call", "silent"),
        ("progress", "peek"),
        ("warn", "trigger"),
        ("error", "trigger"),
        ("idle_detected", "trigger"),
    ])
    def test_default_classifier(self, event, expected):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        assert obs._classify(event, {}) == expected

    def test_tool_result_ok_is_silent(self):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        assert obs._classify("tool_result", {"status": "ok"}) == "silent"

    def test_tool_result_error_is_trigger(self):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        assert obs._classify("tool_result", {"status": "error"}) == "trigger"

    def test_query_engager_unknown_is_trigger(self):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        assert obs._classify("query_engager", {"status": "unknown"}) == "trigger"


class TestRateLimit:
    def test_third_interject_still_allowed(self):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        assert obs._rate_limit_ok("e1") is True
        obs._count_interjection("e1")
        assert obs._rate_limit_ok("e1") is True
        obs._count_interjection("e1")
        obs._count_interjection("e1")
        assert obs._rate_limit_ok("e1") is False


class TestSilent:
    def test_silence_suppresses_triggers(self):
        from observer import Observer

        obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                       model_name="haiku")
        obs.silence("e1")
        assert obs.is_silenced("e1") is True


class TestInterject:
    async def test_interject_posts_notification_when_llm_says_yes(self, monkeypatch):
        from observer import Observer

        bus = MagicMock(); bus.notify = AsyncMock()
        registry = MagicMock()
        registry.get.return_value = MagicMock(
            id="e1", task="Plan Q2", origin={"role": "assistant",
                                              "channel": "telegram",
                                              "chat_id": "c1"},
            role_or_type="finance",
        )
        obs = Observer(bus=bus, engagement_registry=registry, model_name="haiku")

        async def _fake_decide(event_type, payload, rec):
            return {"interject": True, "text": "You may want to check on this."}
        monkeypatch.setattr(obs, "_decide_interjection", _fake_decide)

        await obs._interject("e1", "error", {"kind": "sdk_error"})
        bus.notify.assert_awaited_once()
        notif = bus.notify.await_args.args[0]
        assert notif.target == "assistant"
        assert "You may want to check on this" in getattr(notif.content, "suggested_text", "") or \
            "You may want to check on this" in str(notif.content)

    async def test_interject_skipped_when_llm_says_no(self, monkeypatch):
        from observer import Observer

        bus = MagicMock(); bus.notify = AsyncMock()
        registry = MagicMock()
        registry.get.return_value = MagicMock(
            id="e1", task="t", origin={"role": "assistant", "channel": "telegram",
                                       "chat_id": "c1"},
            role_or_type="finance",
        )
        obs = Observer(bus=bus, engagement_registry=registry, model_name="haiku")
        monkeypatch.setattr(obs, "_decide_interjection",
                            AsyncMock(return_value={"interject": False, "text": ""}))
        await obs._interject("e1", "warn", {})
        bus.notify.assert_not_called()
