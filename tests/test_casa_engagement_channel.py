"""Tests for the casa-engagement-channel MCP server skeleton (v0.37.0 Phase 1).

These tests cover the Phase 1 surface: a single ``reply`` tool that POSTs to
``/internal/channel/send_to_topic`` on the casa-main Unix socket, plus
``declared_capabilities()`` returning the ``claude/channel`` experimental
capability that the bootstrap injects into MCP initialization options.

The D2 contract locked behavior: ``reply`` accepts a ``chat_id`` arg for SDK
compatibility but never forwards it. Routing always uses the engagement_id
captured at module import time from the ``--engagement-id`` CLI flag.
"""

from __future__ import annotations

import importlib
import sys

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def channel_server():
    """Force a fresh re-import of the channel server module per test.

    Uses the explicit ``_configure_for_test`` seam to populate
    ``ENGAGEMENT_ID`` directly, avoiding argv-driven magic. Also installs the
    Phase 2 permission-notification handler on the inner low-level Server so
    tests can assert end-to-end registration without exercising
    ``_run_with_channel_capabilities`` (which would block on stdio).
    """
    sys.modules.pop("channels.casa_engagement_channel", None)
    module = importlib.import_module("channels.casa_engagement_channel")
    module._configure_for_test("deadbeef-test")
    assert module.ENGAGEMENT_ID == "deadbeef-test"
    module._register_permission_notification_handler()
    yield module
    sys.modules.pop("channels.casa_engagement_channel", None)


class TestChannelServerSkeleton:
    async def test_reply_tool_registered(self, channel_server):
        tools = await channel_server._list_tools_for_tests()
        names = [t.name for t in tools]
        assert "reply" in names

    async def test_capability_includes_claude_channel(self, channel_server):
        caps = channel_server.declared_capabilities()
        assert isinstance(caps, dict)
        assert "claude/channel" in caps

    async def test_capability_includes_claude_channel_permission(self, channel_server):
        """Phase 2 (Task 17): claude/channel/permission added to capabilities."""
        caps = channel_server.declared_capabilities()
        assert "claude/channel/permission" in caps

    async def test_initialization_options_carry_experimental_capabilities(
        self, channel_server,
    ):
        """End-to-end (§A.6.1): the bootstrap injects experimental caps
        into the inner Server's InitializationOptions."""
        low_level = channel_server._resolve_mcp_server()
        assert low_level is not None
        opts = low_level.create_initialization_options(
            experimental_capabilities=channel_server.declared_capabilities(),
        )
        experimental = opts.capabilities.experimental or {}
        assert "claude/channel" in experimental
        assert "claude/channel/permission" in experimental

    async def test_reply_forwards_to_internal_socket(
        self, channel_server, monkeypatch,
    ):
        captured: list[tuple[str, dict]] = []

        async def fake_post(path, payload):
            captured.append((path, dict(payload)))
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        result = await channel_server._invoke_tool_for_tests(
            "reply", {"chat_id": "ignored", "text": "hello"},
        )

        assert len(captured) == 1
        path, payload = captured[0]
        assert path == "/internal/channel/send_to_topic"
        assert payload.get("engagement_id") == "deadbeef-test"
        assert payload.get("text") == "hello"
        assert result == {"ok": True}

    async def test_reply_d2_ignores_chat_id_arg(
        self, channel_server, monkeypatch,
    ):
        captured: list[dict] = []

        async def fake_post(path, payload):
            captured.append(dict(payload))
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        await channel_server._invoke_tool_for_tests(
            "reply", {"chat_id": "11111", "text": "one"},
        )
        await channel_server._invoke_tool_for_tests(
            "reply", {"chat_id": "99999", "text": "two"},
        )

        assert len(captured) == 2
        for payload in captured:
            assert "chat_id" not in payload
            assert payload.get("engagement_id") == "deadbeef-test"
        assert captured[0]["text"] == "one"
        assert captured[1]["text"] == "two"


class TestPermissionRequest:
    """Phase 2 Task 18: permission_request notification → inline keyboard."""

    async def test_perm_buttons_callback_data_within_64_bytes(self, channel_server):
        """U1 §9 failure mode: callback_data must fit Telegram's 64-byte limit."""
        buttons = channel_server._build_perm_buttons("a" * 32)
        for row in buttons:
            for btn in row:
                assert len(btn["callback_data"].encode("utf-8")) <= 64

    async def test_perm_buttons_raise_when_request_id_blows_budget(
        self, channel_server,
    ):
        """Defensive: oversized request_id → ValueError, not silent truncation."""
        import pytest
        with pytest.raises(ValueError, match="exceeds"):
            channel_server._build_perm_buttons("x" * 100)

    async def test_perm_buttons_shape_matches_spec(self, channel_server):
        buttons = channel_server._build_perm_buttons("rid-001")
        assert buttons == [[
            {"text": "✅ Allow", "callback_data": "perm:allow:rid-001"},
            {"text": "❌ Deny", "callback_data": "perm:deny:rid-001"},
        ]]

    async def test_permission_request_renders_inline_keyboard(
        self, channel_server, monkeypatch,
    ):
        """U1: permission_request notification posts an inline-keyboard prompt."""
        sent = []

        async def fake_post(path, payload):
            sent.append((path, dict(payload)))
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        await channel_server.handle_permission_request({
            "request_id": "rid-001",
            "tool_name": "Bash",
            "description": "create github repo bonzanni/casa-probe-foo",
            "input_preview": "gh repo create bonzanni/casa-probe-foo --public",
        })

        keyboard_calls = [
            (p, pl) for p, pl in sent
            if p == "/internal/channel/post_inline_keyboard"
        ]
        assert keyboard_calls, f"got paths: {[p for p, _ in sent]}"
        path, payload = keyboard_calls[0]
        assert payload["request_id"] == "rid-001"
        assert "Bash" in payload["text"]
        assert payload["buttons"] == [[
            {"text": "✅ Allow", "callback_data": "perm:allow:rid-001"},
            {"text": "❌ Deny", "callback_data": "perm:deny:rid-001"},
        ]]

    async def test_permission_request_missing_optional_fields(
        self, channel_server, monkeypatch,
    ):
        """U1: description / input_preview are optional — handler tolerates absence."""
        sent = []

        async def fake_post(path, payload):
            sent.append((path, dict(payload)))
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        await channel_server.handle_permission_request({
            "request_id": "rid-min", "tool_name": "Bash",
        })
        keyboard_call = next(
            (p, pl) for p, pl in sent
            if p == "/internal/channel/post_inline_keyboard"
        )
        _, payload = keyboard_call
        assert payload["request_id"] == "rid-min"

    async def test_notification_handler_registered_on_inner_server(
        self, channel_server,
    ):
        """The PermissionRequestNotification class is registered on the
        low-level inner server's notification_handlers map."""
        low_level = channel_server._resolve_mcp_server()
        assert low_level is not None
        cls = channel_server.PermissionRequestNotification
        assert cls in low_level.notification_handlers

    async def test_widened_notification_model_is_clientnotification_subclass(
        self, channel_server,
    ):
        """Regression for the 2026-05-12 dispatch bug: a free
        ``RootModel[Union[...]]`` parses validation but ``Server._handle_message``
        match-cases on ``types.ClientNotification(...)`` — anything that isn't
        a ClientNotification subclass is silently dropped. Verified live on
        N150 stdio probe before this guard test was added.
        """
        from mcp.types import ClientNotification
        wider = channel_server._build_wider_notification_root_model()
        assert issubclass(wider, ClientNotification)
        v = wider.model_validate({
            "method": "notifications/claude/channel/permission_request",
            "params": {"request_id": "rid-1", "tool_name": "Bash"},
        })
        assert isinstance(v, ClientNotification)
        assert type(v.root) is channel_server.PermissionRequestNotification


class TestPermissionVerdictEmission:
    """Phase 2 Task 21: drain queue + emit notifications/claude/channel/permission."""

    async def test_emit_permission_notification_sends_via_session(
        self, channel_server,
    ):
        from unittest.mock import AsyncMock
        fake_session = AsyncMock()
        channel_server._CURRENT_SESSION = fake_session

        await channel_server._emit_permission_notification({
            "request_id": "rid-7", "verdict": "allow", "operator_id": 42,
        })

        fake_session.send_notification.assert_awaited_once()
        sent = fake_session.send_notification.await_args.args[0]
        # Wire-shape check via model_dump (what BaseSession.send_notification
        # uses internally).
        dumped = sent.model_dump(by_alias=True, mode="json", exclude_none=True)
        assert dumped == {
            "method": "notifications/claude/channel/permission",
            "params": {"request_id": "rid-7", "verdict": "allow"},
        }

    async def test_emit_permission_notification_drops_operator_id(
        self, channel_server,
    ):
        """operator_id is intentionally stripped from the wire payload — Claude
        only needs request_id + verdict to resume the gated tool call."""
        from unittest.mock import AsyncMock
        fake_session = AsyncMock()
        channel_server._CURRENT_SESSION = fake_session

        await channel_server._emit_permission_notification({
            "request_id": "rid-1", "verdict": "deny", "operator_id": 999,
        })
        sent = fake_session.send_notification.await_args.args[0]
        assert "operator_id" not in sent.params

    async def test_emit_permission_notification_handles_missing_session(
        self, channel_server, caplog,
    ):
        """If no session is live (e.g. shutdown race), emit logs + drops."""
        channel_server._CURRENT_SESSION = None
        # Should not raise.
        await channel_server._emit_permission_notification({
            "request_id": "rid-1", "verdict": "allow",
        })


class TestU3StateTransitionsFromChannelServer:
    """Phase 2 Task 23: channel server flips state on permission_request +
    verdict via /internal/channel/update_state."""

    async def test_permission_request_flips_to_awaiting_before_keyboard(
        self, channel_server, monkeypatch,
    ):
        sent: list[tuple[str, dict]] = []

        async def fake_post(path, payload):
            sent.append((path, dict(payload)))
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)
        await channel_server.handle_permission_request({
            "request_id": "rid-1", "tool_name": "Bash",
        })

        # Order matters: U3 flip first (visible to operator), then keyboard.
        paths = [p for p, _ in sent]
        assert paths.index("/internal/channel/update_state") < paths.index(
            "/internal/channel/post_inline_keyboard",
        )
        update_payload = next(
            pl for p, pl in sent if p == "/internal/channel/update_state"
        )
        assert update_payload["new_state"] == "awaiting"

    async def test_emit_permission_notification_flips_back_to_active(
        self, channel_server, monkeypatch,
    ):
        from unittest.mock import AsyncMock
        fake_session = AsyncMock()
        channel_server._CURRENT_SESSION = fake_session
        sent: list[tuple[str, dict]] = []

        async def fake_post(path, payload):
            sent.append((path, dict(payload)))
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", fake_post)

        await channel_server._emit_permission_notification({
            "request_id": "rid-2", "verdict": "allow",
        })

        # Notification fired and state flipped back to active.
        fake_session.send_notification.assert_awaited_once()
        active_calls = [
            (p, pl) for p, pl in sent
            if p == "/internal/channel/update_state"
            and pl.get("new_state") == "active"
        ]
        assert active_calls, f"got calls: {sent}"

    async def test_state_transition_post_failure_is_swallowed(
        self, channel_server, monkeypatch,
    ):
        """A transient state-update failure must not block the verdict path."""
        from unittest.mock import AsyncMock
        fake_session = AsyncMock()
        channel_server._CURRENT_SESSION = fake_session

        async def exploding_post(path, payload):
            if path == "/internal/channel/update_state":
                raise RuntimeError("socket gone")
            return {"ok": True}

        monkeypatch.setattr(channel_server, "_internal_post", exploding_post)
        # Should not raise.
        await channel_server._emit_permission_notification({
            "request_id": "rid-3", "verdict": "deny",
        })
        # Verdict still delivered.
        fake_session.send_notification.assert_awaited_once()
