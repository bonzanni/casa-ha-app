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
