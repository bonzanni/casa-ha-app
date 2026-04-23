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
