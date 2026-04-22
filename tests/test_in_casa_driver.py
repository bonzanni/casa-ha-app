"""Tests for InCasaDriver — start, send_user_turn, cancel, is_alive."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock

pytestmark = pytest.mark.asyncio


def _mk_text_block(text: str) -> TextBlock:
    """Instantiate TextBlock regardless of SDK shape."""
    try:
        return TextBlock(text=text)
    except TypeError:
        return TextBlock(text)  # type: ignore[call-arg]


def _mk_assistant(text: str) -> AssistantMessage:
    """Instantiate AssistantMessage regardless of SDK shape."""
    block = _mk_text_block(text)
    try:
        return AssistantMessage(content=[block])
    except TypeError:
        m = AssistantMessage.__new__(AssistantMessage)
        m.content = [block]  # type: ignore[attr-defined]
        return m


def _make_record(**overrides):
    from engagement_registry import EngagementRecord

    base = dict(
        id="e1", kind="specialist", role_or_type="finance", driver="in_casa",
        status="active", topic_id=42,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )
    base.update(overrides)
    return EngagementRecord(**base)


class TestInCasaStart:
    async def test_start_opens_client_and_marks_alive(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        ctor_calls = []

        class _FakeClient:
            def __init__(self, options):
                ctor_calls.append(options)
                self._connected = False

            async def __aenter__(self):
                self._connected = True
                return self

            async def __aexit__(self, *args):
                self._connected = False

            async def query(self, prompt):
                pass

            async def receive_response(self):
                if False:
                    yield None  # pragma: no cover

            async def close(self):
                self._connected = False

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        channel_sender = AsyncMock()
        drv = InCasaDriver(send_to_topic=channel_sender)
        rec = _make_record()

        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="sonnet"))
        assert drv.is_alive(rec) is True
        assert len(ctor_calls) == 1

    async def test_start_posts_initial_response_to_topic(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_assistant("Hello from Alex")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        sender = AsyncMock()
        drv = InCasaDriver(send_to_topic=sender)
        rec = _make_record()

        await drv.start(rec, prompt="hi", options=ClaudeAgentOptions(model="sonnet"))
        sender.assert_awaited_once_with(42, "Hello from Alex")


class TestInCasaSendUserTurn:
    async def test_send_user_turn_streams_reply_to_topic(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        turns = []

        class _FakeClient:
            def __init__(self, options): self._q = []
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt):
                turns.append(prompt)
            async def receive_response(self):
                yield _mk_assistant(f"re:{turns[-1]}")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        sender = AsyncMock()
        drv = InCasaDriver(send_to_topic=sender)
        rec = _make_record()
        await drv.start(rec, "system prompt", ClaudeAgentOptions(model="sonnet"))
        turns.clear()  # reset after start's initial delivery

        await drv.send_user_turn(rec, "user said X")
        assert turns == ["user said X"]
        assert sender.await_count >= 1
        last_call = sender.await_args_list[-1]
        assert last_call.args == (42, "re:user said X")

    async def test_send_user_turn_raises_when_not_alive(self):
        from drivers.in_casa_driver import InCasaDriver, DriverNotAliveError

        drv = InCasaDriver(send_to_topic=AsyncMock())
        rec = _make_record()
        with pytest.raises(DriverNotAliveError):
            await drv.send_user_turn(rec, "x")


class TestInCasaCancel:
    async def test_cancel_closes_client_and_flips_alive(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        close_calls = []

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                if False:
                    yield None  # pragma: no cover
            async def close(self):
                close_calls.append(1)

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        drv = InCasaDriver(send_to_topic=AsyncMock())
        rec = _make_record()
        await drv.start(rec, "p", ClaudeAgentOptions(model="sonnet"))
        assert drv.is_alive(rec) is True
        await drv.cancel(rec)
        assert drv.is_alive(rec) is False
        assert close_calls == [1]

    async def test_cancel_is_idempotent(self):
        from drivers.in_casa_driver import InCasaDriver

        drv = InCasaDriver(send_to_topic=AsyncMock())
        rec = _make_record()
        # Not alive yet: must not raise.
        await drv.cancel(rec)
        await drv.cancel(rec)
        assert drv.is_alive(rec) is False
