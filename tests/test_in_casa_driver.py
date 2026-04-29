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


class TestInCasaResume:
    async def test_resume_reopens_client_with_session_id(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        seen_resume = []

        class _FakeClient:
            def __init__(self, options):
                seen_resume.append(getattr(options, "resume", None))
                self.session_id = "sess-new"
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                if False:
                    yield None  # pragma: no cover
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        drv = InCasaDriver(send_to_topic=AsyncMock())
        rec = _make_record(sdk_session_id="sess-old")
        await drv.resume(rec, session_id="sess-old")
        assert drv.is_alive(rec) is True
        assert seen_resume == ["sess-old"]

    async def test_get_session_id_returns_clients_session_id(self, monkeypatch):
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options):
                self.session_id = "sess-xyz"
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                if False:
                    yield None  # pragma: no cover
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        drv = InCasaDriver(send_to_topic=AsyncMock())
        rec = _make_record()
        await drv.start(rec, "p", ClaudeAgentOptions(model="sonnet"))
        assert drv.get_session_id(rec) == "sess-xyz"


class TestInCasaEngagementContext:
    async def test_deliver_turn_sets_engagement_var_during_sdk_loop(self, monkeypatch):
        """engagement_var is bound for the duration of receive_response()."""
        from drivers.in_casa_driver import InCasaDriver
        from tools import engagement_var

        captured: list = []

        class _FakeClient:
            def __init__(self, options): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass

            async def receive_response(self):
                # Snapshot engagement_var while inside the loop
                captured.append(engagement_var.get(None))
                yield _mk_assistant("hi")

            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        drv = InCasaDriver(send_to_topic=AsyncMock())
        rec = _make_record(role_or_type="configurator")

        # Pre-state: unbound
        assert engagement_var.get(None) is None
        await drv.start(rec, "hi", ClaudeAgentOptions(model="sonnet"))
        # Post-state: reset
        assert engagement_var.get(None) is None
        # During-state: was bound to rec
        assert captured == [rec]

    async def test_deliver_turn_persists_session_id_on_first_message(self, monkeypatch):
        """First non-null client.session_id triggers persist_session_id once."""
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options):
                self.session_id = "sess-abc"
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_assistant("hi")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        persist = AsyncMock()
        drv = InCasaDriver(send_to_topic=AsyncMock(), persist_session_id=persist)
        rec = _make_record()

        await drv.start(rec, "hi", ClaudeAgentOptions(model="sonnet"))

        persist.assert_awaited_once_with(rec.id, "sess-abc")
        assert rec.sdk_session_id == "sess-abc"

    async def test_deliver_turn_persist_idempotent_on_second_turn(self, monkeypatch):
        """Subsequent _deliver_turn calls skip the callback when sid unchanged."""
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options):
                self.session_id = "sess-abc"
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_assistant("hi")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        persist = AsyncMock()
        drv = InCasaDriver(send_to_topic=AsyncMock(), persist_session_id=persist)
        rec = _make_record()

        await drv.start(rec, "hi", ClaudeAgentOptions(model="sonnet"))
        await drv.send_user_turn(rec, "another turn")

        # Persist must fire exactly once across both turns.
        assert persist.await_count == 1
        assert rec.sdk_session_id == "sess-abc"

    async def test_deliver_turn_persist_callback_optional(self, monkeypatch):
        """Driver constructed with default persist_session_id=None runs cleanly."""
        from drivers.in_casa_driver import InCasaDriver

        class _FakeClient:
            def __init__(self, options):
                self.session_id = "sess-abc"
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def query(self, prompt): pass
            async def receive_response(self):
                yield _mk_assistant("hi")
            async def close(self): pass

        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

        # No persist_session_id supplied — uses default None.
        drv = InCasaDriver(send_to_topic=AsyncMock())
        rec = _make_record()

        await drv.start(rec, "hi", ClaudeAgentOptions(model="sonnet"))
        # Sanity: rec.sdk_session_id stays None because no callback was wired
        # AND we don't write the in-place value when callback is None
        # (Task-2 implementation note below).
        assert rec.sdk_session_id is None
