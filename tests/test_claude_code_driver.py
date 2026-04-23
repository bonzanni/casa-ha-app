"""Tests for ClaudeCodeDriver — s6-rc orchestration + workspace + FIFO."""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _make_defn(tmp_path, plugins=None):
    from config import ExecutorDefinition

    exec_dir = tmp_path / "defaults-executors" / "hello-driver"
    exec_dir.mkdir(parents=True)
    (exec_dir / "prompt.md").write_text("You are hello-driver. Task: {task}.")
    plugins_dir = ""
    if plugins is not None:
        pdir = exec_dir / "plugins"
        pdir.mkdir()
        for p in plugins:
            (pdir / p).mkdir()
        plugins_dir = str(pdir)
    return ExecutorDefinition(
        type="hello-driver",
        description="Test harness executor type for claude_code driver.",
        model="sonnet",
        driver="claude_code",
        enabled=False,
        tools_allowed=["mcp__casa-framework__emit_completion"],
        permission_mode="dontAsk",
        mcp_server_names=["casa-framework"],
        prompt_template_path=str(exec_dir / "prompt.md"),
        plugins_dir=plugins_dir,
    )


def _make_record():
    from engagement_registry import EngagementRecord
    return EngagementRecord(
        id="abc12345def67890", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=999,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None,
        origin={"channel": "telegram", "chat_id": "42"}, task="say hello",
    )


class TestStart:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_start_provisions_writes_service_compiles_starts(self, monkeypatch, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        # Mock every s6_rc subprocess call to avoid actually running s6-rc-compile.
        calls: list[tuple[str, dict]] = []

        async def fake_cau():
            calls.append(("compile_and_update_locked", {}))
        async def fake_start(engagement_id):
            calls.append(("start_service", {"engagement_id": engagement_id}))

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        # start_service in impl uses kw-only "engagement_id"; wrap accordingly.
        async def fake_start_kw(*, engagement_id):
            await fake_start(engagement_id)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_kw)

        # Redirect s6_rc.ENGAGEMENT_SOURCES_ROOT to a tmp dir.
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT",
                            str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()

        defn = _make_defn(tmp_path)
        rec = _make_record()

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            base_plugins_root=str(tmp_path / "base-plugins"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()
        (tmp_path / "base-plugins").mkdir()

        await drv.start(rec, prompt="system prompt body", options=defn)

        # Compile must run BEFORE start_service
        names = [c[0] for c in calls]
        assert names == ["compile_and_update_locked", "start_service"]
        # Service dir written with the correct engagement id
        assert (tmp_path / "svc-root" / f"engagement-{rec.id}").is_dir()
        assert (tmp_path / "svc-root" / f"engagement-{rec.id}" / "run").is_file()
        # Workspace provisioned
        assert (tmp_path / "engagements" / rec.id / "CLAUDE.md").exists()
        assert (tmp_path / "engagements" / rec.id / "stdin.fifo").exists()


class TestURLCapture:
    async def test_first_url_line_posts_to_topic(self, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver

        sender = AsyncMock()
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path), base_plugins_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
        )
        log = tmp_path / "log"
        log.write_text("boot...\nRemote Control URL: https://r.test/abc\nmore\n")

        rec = _make_record()
        # capture_url reads new lines as they appear — for a test, we pre-write
        # then run the coroutine with a short time budget.
        task = asyncio.create_task(
            drv._capture_url(rec, log_path=str(log), initial_window_s=1.0)
        )
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # send_to_topic was called with the topic_id + a remote-control message.
        sender.assert_awaited()
        args, _ = sender.call_args
        assert args[0] == rec.topic_id
        assert "https://r.test/abc" in args[1]

    async def test_duplicate_url_not_reposted(self, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver

        sender = AsyncMock()
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path), base_plugins_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
        )
        log = tmp_path / "log"
        # Same URL twice
        log.write_text(
            "Remote Control URL: https://r.test/same\n"
            "filler\n"
            "Remote Control URL: https://r.test/same\n"
        )

        rec = _make_record()
        task = asyncio.create_task(
            drv._capture_url(rec, log_path=str(log), initial_window_s=1.0)
        )
        await asyncio.sleep(0.3)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

        # Only one post, not two
        assert sender.await_count == 1


class TestRespawnPoller:
    async def test_emits_bus_event_on_pid_change(self, monkeypatch, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        pids = iter([100, 100, 200, 200, 200])
        async def fake_pid(*, engagement_id):
            try:
                return next(pids)
            except StopIteration:
                return 200
        monkeypatch.setattr(s6_rc, "service_pid", fake_pid)

        bus_events: list[dict] = []
        async def fake_publish(*args, **kwargs):
            bus_events.append({"args": args, "kwargs": kwargs})

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path), base_plugins_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        drv._publish_bus_event = fake_publish     # dependency injection

        rec = _make_record()
        task = asyncio.create_task(
            drv._poll_respawns(rec, interval_s=0.05)
        )
        await asyncio.sleep(0.4)        # enough ticks to see the 100 → 200 change
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

        # At least one subprocess_respawn event with previous=100, new=200
        respawn = [e for e in bus_events if
                   e["args"][0].get("event") == "subprocess_respawn"]
        assert len(respawn) >= 1
        assert respawn[0]["args"][0]["previous_pid"] == 100
        assert respawn[0]["args"][0]["new_pid"] == 200


class TestCancel:
    async def test_cancel_stops_service_and_removes_dir(self, monkeypatch, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        stopped: list[str] = []
        async def fake_stop(*, engagement_id):
            stopped.append(engagement_id)
        async def fake_cau(): pass

        monkeypatch.setattr(s6_rc, "stop_service", fake_stop)
        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc"))
        (tmp_path / "svc").mkdir()
        (tmp_path / "svc" / "engagement-abc12345def67890").mkdir()
        (tmp_path / "svc" / "engagement-abc12345def67890" / "type").write_text("longrun\n")

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "eng"), base_plugins_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        await drv.cancel(rec)

        assert stopped == [rec.id]
        assert not (tmp_path / "svc" / f"engagement-{rec.id}").exists()
