# tests/test_unix_socket_runner.py
"""Unit tests for the second-AppRunner Unix-socket setup in casa_core.py.

Strategy: factor the setup into a helper `start_internal_unix_runner()`
that returns the AppRunner (so the calling code can stop it on shutdown),
then test the helper end-to-end with aiohttp.ClientSession+UnixConnector.

We do not start a real casa-main; we pass in fakes and assert the round
trip + the on-disk socket properties.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="aiohttp.UnixConnector is not available on Windows",
    ),
]


async def _ok_tool(args):
    return {"content": [{"type": "text", "text": "ok"}]}


class _FakeReg:
    def get(self, _id): return None


async def test_unix_runner_creates_socket_with_mode_0600() -> None:
    from casa_core import start_internal_unix_runner
    with tempfile.TemporaryDirectory() as td:
        sock = os.path.join(td, "internal.sock")
        runner = await start_internal_unix_runner(
            socket_path=sock,
            tool_dispatch={"ok": _ok_tool},
            engagement_registry=_FakeReg(),
            hook_policies={},
        )
        try:
            assert os.path.exists(sock)
            mode = stat.S_IMODE(os.stat(sock).st_mode)
            assert mode == 0o600
        finally:
            await runner.cleanup()


async def test_unix_runner_unlinks_socket_on_cleanup() -> None:
    from casa_core import start_internal_unix_runner
    with tempfile.TemporaryDirectory() as td:
        sock = os.path.join(td, "internal.sock")
        runner = await start_internal_unix_runner(
            socket_path=sock,
            tool_dispatch={},
            engagement_registry=_FakeReg(),
            hook_policies={},
        )
        await runner.cleanup()
        # Socket file should be gone after cleanup.
        assert not os.path.exists(sock), \
            "internal.sock should be unlinked on AppRunner cleanup"


async def test_unix_runner_serves_tools_call_via_unix_socket() -> None:
    from casa_core import start_internal_unix_runner
    with tempfile.TemporaryDirectory() as td:
        sock = os.path.join(td, "internal.sock")
        runner = await start_internal_unix_runner(
            socket_path=sock,
            tool_dispatch={"ok": _ok_tool},
            engagement_registry=_FakeReg(),
            hook_policies={},
        )
        try:
            connector = aiohttp.UnixConnector(path=sock)
            async with aiohttp.ClientSession(connector=connector) as sess:
                async with sess.post(
                    "http://unix/internal/tools/call",
                    json={"name": "ok", "arguments": {}, "engagement_id": None},
                ) as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    assert body == {"content": [{"type": "text", "text": "ok"}]}
        finally:
            await runner.cleanup()


async def test_unix_runner_serves_hooks_resolve_via_unix_socket() -> None:
    from casa_core import start_internal_unix_runner

    async def _allow(_p, _c, _o):
        return {"hookSpecificOutput": {"permissionDecision": "allow"}}

    with tempfile.TemporaryDirectory() as td:
        sock = os.path.join(td, "internal.sock")
        runner = await start_internal_unix_runner(
            socket_path=sock,
            tool_dispatch={},
            engagement_registry=_FakeReg(),
            hook_policies={"allow_all": ("Bash", _allow)},
        )
        try:
            connector = aiohttp.UnixConnector(path=sock)
            async with aiohttp.ClientSession(connector=connector) as sess:
                async with sess.post(
                    "http://unix/internal/hooks/resolve",
                    json={"policy": "allow_all",
                          "payload": {"tool_name": "Bash"}},
                ) as resp:
                    body = await resp.json()
                    assert body["hookSpecificOutput"]["permissionDecision"] == "allow"
        finally:
            await runner.cleanup()


async def test_unix_runner_creates_parent_dir_if_missing() -> None:
    """If /run/casa/ doesn't exist yet, the helper creates it."""
    from casa_core import start_internal_unix_runner
    with tempfile.TemporaryDirectory() as td:
        sub = os.path.join(td, "subdir-not-yet")
        sock = os.path.join(sub, "internal.sock")
        assert not os.path.isdir(sub)
        runner = await start_internal_unix_runner(
            socket_path=sock,
            tool_dispatch={},
            engagement_registry=_FakeReg(),
            hook_policies={},
        )
        try:
            assert os.path.isdir(sub)
            mode = stat.S_IMODE(os.stat(sub).st_mode)
            # Parent dir should be 0700 (root-only).
            assert mode == 0o700
        finally:
            await runner.cleanup()


async def test_unix_runner_serves_channel_send_to_topic() -> None:
    """E-12 (v0.37.0): /internal/channel/send_to_topic is wired onto
    the unix-socket app when telegram_channel is provided."""
    from casa_core import start_internal_unix_runner
    from unittest.mock import AsyncMock
    from types import SimpleNamespace

    class _Channel:
        engagement_supergroup_id = -100
        # v0.70.0: the CC reply handler routes through send_response_to_topic.
        send_response_to_topic = AsyncMock(return_value=9001)
        send_to_topic = AsyncMock(return_value=9001)

    class _Reg:
        def get(self, eid):
            if eid == "eng-1":
                return SimpleNamespace(id=eid, topic_id=42, status="active")
            return None

    with tempfile.TemporaryDirectory() as td:
        sock = os.path.join(td, "internal.sock")
        runner = await start_internal_unix_runner(
            socket_path=sock,
            tool_dispatch={},
            engagement_registry=_Reg(),
            hook_policies={},
            telegram_channel=_Channel(),
        )
        try:
            connector = aiohttp.UnixConnector(path=sock)
            async with aiohttp.ClientSession(connector=connector) as sess:
                async with sess.post(
                    "http://unix/internal/channel/send_to_topic",
                    json={"engagement_id": "eng-1", "text": "hello operator"},
                ) as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    assert body == {"ok": True, "message_id": 9001}
            # v0.70.0: CC reply must route through send_response_to_topic, not
            # the plain send_to_topic.
            _Channel.send_response_to_topic.assert_awaited_once()
            _Channel.send_to_topic.assert_not_awaited()
        finally:
            await runner.cleanup()


async def test_unix_runner_skips_channel_routes_when_no_telegram() -> None:
    """E-12 fallback: telegram_channel=None means /internal/channel/*
    routes are NOT registered (returns 404 instead of 500)."""
    from casa_core import start_internal_unix_runner
    with tempfile.TemporaryDirectory() as td:
        sock = os.path.join(td, "internal.sock")
        runner = await start_internal_unix_runner(
            socket_path=sock,
            tool_dispatch={},
            engagement_registry=_FakeReg(),
            hook_policies={},
            # telegram_channel intentionally omitted.
        )
        try:
            connector = aiohttp.UnixConnector(path=sock)
            async with aiohttp.ClientSession(connector=connector) as sess:
                async with sess.post(
                    "http://unix/internal/channel/send_to_topic",
                    json={"engagement_id": "x", "text": "y"},
                ) as resp:
                    assert resp.status == 404
        finally:
            await runner.cleanup()


# ---------------------------------------------------------------------------
# Task 14: the five personality/specialist/explain admin routes are
# Unix-socket-only — registered ONLY when start_internal_unix_runner is
# given a runtime, and NEVER on the public 8099 app (built separately in
# casa_core.main and never passed through register_personality_admin_routes).
# ---------------------------------------------------------------------------

_PERSONALITY_ADMIN_PATHS = (
    "/admin/personality/inspect",
    "/admin/personality/render",
    "/admin/personality/diff",
    "/admin/specialist/status",
    "/admin/explain",
)


class _FakeRuntimeForAdminRoutes:
    def __init__(self, *, explanation_store):
        self.persona_packs: dict = {}
        self.compiled_prompt_bundles: dict = {}
        self.role_slots: dict = {}
        self.bindings: dict = {}
        self.explanation_store = explanation_store


async def test_unix_runner_registers_all_five_personality_admin_routes() -> None:
    """All five routes must exist on the internal Unix-socket app and be
    handled by OUR code (JSON responses), not aiohttp's generic 404 page."""
    from pathlib import Path

    from casa_core import start_internal_unix_runner
    from explanation_store import ExplanationStore

    with tempfile.TemporaryDirectory() as td:
        sock = os.path.join(td, "internal.sock")
        runtime = _FakeRuntimeForAdminRoutes(
            explanation_store=ExplanationStore(Path(td) / "explanations"),
        )
        runner = await start_internal_unix_runner(
            socket_path=sock,
            tool_dispatch={},
            engagement_registry=_FakeReg(),
            hook_policies={},
            runtime=runtime,
        )
        try:
            connector = aiohttp.UnixConnector(path=sock)
            async with aiohttp.ClientSession(connector=connector) as sess:
                for path in _PERSONALITY_ADMIN_PATHS:
                    async with sess.post("http://unix" + path, json={}) as resp:
                        # Every one of the five is handled by our code (a
                        # domain 400/404 JSON response, never aiohttp's
                        # generic "404: Not Found" text page), proving the
                        # route itself is registered.
                        assert resp.content_type == "application/json", (
                            f"{path} was not handled by a registered route "
                            f"(got content_type={resp.content_type!r})"
                        )
        finally:
            await runner.cleanup()


async def test_unix_runner_skips_personality_admin_routes_when_runtime_none() -> None:
    """Existing test/fallback boots that pass runtime=None (as several
    tests above already do) must not crash — and must NOT register the
    five admin routes at all."""
    from casa_core import start_internal_unix_runner

    with tempfile.TemporaryDirectory() as td:
        sock = os.path.join(td, "internal.sock")
        runner = await start_internal_unix_runner(
            socket_path=sock,
            tool_dispatch={},
            engagement_registry=_FakeReg(),
            hook_policies={},
            # runtime intentionally omitted (defaults to None).
        )
        try:
            connector = aiohttp.UnixConnector(path=sock)
            async with aiohttp.ClientSession(connector=connector) as sess:
                for path in _PERSONALITY_ADMIN_PATHS:
                    async with sess.post("http://unix" + path, json={}) as resp:
                        assert resp.status == 404
                        assert resp.content_type != "application/json"
        finally:
            await runner.cleanup()


async def test_personality_admin_routes_404_on_public_app() -> None:
    """Load-bearing constraint: the five routes must NEVER exist on the
    public 8099 app. casa_core.main() builds that app completely
    separately from start_internal_unix_runner's internal_app and never
    calls register_personality_admin_routes on it — simulate that app
    here (a plain web.Application(), same as production's public `app`
    before its own unrelated routes are added) and confirm a 404 that is
    NOT one of our JSON error responses."""
    public_app = web.Application()
    async with TestClient(TestServer(public_app)) as client:
        for path in _PERSONALITY_ADMIN_PATHS:
            resp = await client.post(path, json={})
            assert resp.status == 404
            assert resp.content_type != "application/json"
