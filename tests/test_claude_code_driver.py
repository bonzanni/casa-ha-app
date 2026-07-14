"""Tests for ClaudeCodeDriver — s6-rc orchestration + workspace + FIFO."""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


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

        # Don't actually spawn the background tasks (log relay, respawn
        # poller, session-id capture) in the unit test — they're covered by
        # their dedicated test classes. Without this patch they poll
        # non-existent paths forever and hang CI.
        monkeypatch.setattr(
            ClaudeCodeDriver, "_spawn_background_tasks",
            lambda self, engagement: None,
        )

        # Don't block on FIFO open — the real FIFO has no reader in this test
        # because the s6 service is mocked away. Bypassing is safe: this test
        # only verifies start() provisioning + dispatch, not FIFO I/O.
        async def _noop_write(self, engagement, text):
            return None
        monkeypatch.setattr(
            ClaudeCodeDriver, "_write_to_fifo", _noop_write,
        )

        defn = _make_defn(tmp_path)
        rec = _make_record()

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
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


class TestStartRollback:
    """Bug 13 (v0.14.6): if any step in start() fails, the partial
    workspace + service-dir + s6-rc compile must be rolled back.

    Pre-fix the workspace was left UNDERGOING and the sweeper skipped it
    forever, leaking disk and producing ghost engagements that boot
    replay would attempt to resurrect.
    """

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="workspace provisioning uses mkfifo/symlink (Linux-only)",
    )
    async def test_start_service_failure_cleans_up(self, monkeypatch, tmp_path):
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        compile_calls: list[str] = []

        async def fake_cau():
            compile_calls.append("compile")

        async def fake_start_fail(*, engagement_id):
            raise RuntimeError("simulated s6-rc start failure")

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "start_service", fake_start_fail)
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT",
                            str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()

        defn = _make_defn(tmp_path)
        rec = _make_record()

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()
        (tmp_path / "base-plugins").mkdir()

        with pytest.raises(RuntimeError, match="simulated s6-rc start failure"):
            await drv.start(rec, prompt="hi", options=defn)

        # The original failure is re-raised.
        # Rollback removed the workspace.
        assert not (tmp_path / "engagements" / rec.id).exists(), (
            "Bug 13: workspace not cleaned up on start_service failure — "
            "leaves an orphan UNDERGOING that the sweeper will skip forever"
        )
        # And the s6 service dir.
        assert not (tmp_path / "svc-root" / f"engagement-{rec.id}").exists(), (
            "Bug 13: s6 service dir not cleaned up on start_service failure"
        )
        # _compile_and_update_locked should be called twice: once forward,
        # once on rollback.
        assert compile_calls.count("compile") == 2

    async def test_provision_failure_cleans_up(self, monkeypatch, tmp_path):
        """Failure during provisioning (before service-dir write) — only
        the workspace tree needs cleanup, not the (never-written) service dir."""
        from drivers import workspace as ws_mod
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        async def fail_provision(**kw):
            # Simulate partial workspace creation, then explode.
            from pathlib import Path
            ws = Path(kw["engagements_root"]) / kw["engagement_id"]
            ws.mkdir(parents=True, exist_ok=False)
            (ws / "CLAUDE.md").write_text("partial", encoding="utf-8")
            raise OSError("disk full")

        monkeypatch.setattr(ws_mod, "provision_workspace", fail_provision)

        # Also patch the imported reference inside the driver module.
        from drivers import claude_code_driver as ccd
        monkeypatch.setattr(ccd, "provision_workspace", fail_provision)

        async def fake_cau():
            pass

        async def fake_start(*, engagement_id):
            pass

        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(s6_rc, "start_service", fake_start)
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT",
                            str(tmp_path / "svc-root"))
        (tmp_path / "svc-root").mkdir()

        defn = _make_defn(tmp_path)
        rec = _make_record()

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://127.0.0.1:8080/mcp/casa-framework",
        )
        (tmp_path / "engagements").mkdir()
        (tmp_path / "base-plugins").mkdir()

        with pytest.raises(OSError, match="disk full"):
            await drv.start(rec, prompt="hi", options=defn)

        # Provisioning aborted before write_casa_meta — but the partial
        # workspace tree should still be removed (rmtree(ignore_errors=True)
        # is best-effort).
        assert not (tmp_path / "engagements" / rec.id).exists()


class TestNoRemoteControlNotices:
    """v0.64.0: URL capture removed — headless claude auto-degrades to
    one-shot --print mode on non-TTY stdout and never prints a
    'Remote Control URL:' line (live-verified 2026-07-10), so the driver
    must neither watch for one nor post any remote-control topic notice.
    See docs/superpowers/specs/2026-07-10-v0.64.0-remote-control-honesty-design.md."""

    async def test_background_tasks_never_post_to_topic(self, tmp_path, caplog):
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        sender = AsyncMock()
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
        )
        rec = _make_record()
        # DEBUG-enable subprocess_cli so the full roster (incl. relay) runs.
        with caplog.at_level(logging.DEBUG, logger="subprocess_cli"):
            drv._spawn_background_tasks(rec)
            tasks = drv._tasks[rec.id]
            # respawn poller + session-id capture + always-on topic relay +
            # DEBUG log relay; no URL capture. (v0.75.0 added the always-on
            # topic relay, so DEBUG-enabled is now 4 tasks not 3.)
            assert len(tasks) == 4
            await asyncio.sleep(0.3)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        sender.assert_not_awaited()


class TestRelaySpawnGate:
    """v0.64.0 efficiency: the DEBUG raw-log relay tails a now-real file at
    10 Hz only to discard every line unless subprocess_cli is DEBUG-enabled —
    so THAT task is only spawned when it is. (A LOG_LEVEL flip requires an
    add-on restart, which respawns these tasks anyway.) v0.75.0: the SEPARATE
    always-on topic-stream relay is spawned regardless of LOG_LEVEL."""

    async def test_relay_skipped_when_debug_disabled(self, tmp_path):
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        lg = logging.getLogger("subprocess_cli")
        old_level = lg.level
        lg.setLevel(logging.WARNING)
        try:
            drv._spawn_background_tasks(rec)
            tasks = drv._tasks[rec.id]
            # respawn poller + session-id capture + always-on topic relay.
            # The DEBUG raw-log relay is skipped; the topic relay is NOT.
            assert len(tasks) == 3
            names = [t.get_name() for t in tasks]
            assert any(n.startswith("topic_relay:") for n in names), names
        finally:
            lg.setLevel(old_level)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class TestTailFileResilience:
    """s6-log rotates `current` by rename; if that lands between the
    tailer's exists() and open(), the open raises FileNotFoundError. The
    relay task is unobserved — the tailer must retry, not die."""

    async def test_survives_transient_open_failure(self, tmp_path, monkeypatch):
        import pathlib
        from drivers.claude_code_driver import _tail_file

        f = tmp_path / "current"
        f.write_text("hello\n", encoding="utf-8")

        real_open = pathlib.Path.open
        state = {"raised": False}

        def flaky(self, *args, **kwargs):
            if self == f and not state["raised"]:
                state["raised"] = True
                raise FileNotFoundError("rotated away")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "open", flaky)

        gen = _tail_file(str(f))
        line = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert line == "hello\n"
        assert state["raised"], "the transient failure was never exercised"
        await gen.aclose()


class TestSessionIdCapture:
    """P31 (v0.37.10): watch claude CLI's own session storage dir for new
    ``<uuid>.jsonl`` files. The filename (minus extension) IS the SDK
    session UUID. Persist to ``<workspace>/.session_id`` atomically so
    boot-replay's ``--resume`` plumbing picks it up after a Casa restart.

    Replaces v0.37.9's s6-log tailing approach which was non-functional
    in production: the s6-rc service dir's log/ subdir lacked the
    producer-for / consumer-for wiring required to compile the
    producer-consumer pair, so ``/var/log/casa-engagement-<id>/current``
    was never created. Live evidence: 2026-05-14 exploration6 — both
    engagements ``28fdeb04`` and ``3e44c2cf`` saw zero session_id writes.

    Claude CLI session storage layout (HOME=<ws>/.home, CWD=<ws>):

        <ws>/.home/.claude/projects/-data-engagements-<id>/<uuid>.jsonl

    The directory-name encoding replaces ``/`` with ``-`` in the
    workspace path (claude CLI native behavior).
    """

    @staticmethod
    def _projects_dir(ws):
        """Mirror the encoding the claude CLI uses for cwd directory names."""
        return ws / ".home" / ".claude" / "projects" / (
            f"-data-engagements-{ws.name}"
        )

    async def test_writes_session_id_on_first_jsonl(self, tmp_path):
        """Happy path: claude CLI creates the projects dir with a single
        ``<uuid>.jsonl`` file. Watcher persists the UUID to
        ``<ws>/.session_id`` and invokes ``persist_session_id`` callback.
        """
        from drivers.claude_code_driver import ClaudeCodeDriver

        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        projects = self._projects_dir(ws)
        projects.mkdir(parents=True)
        sid = "8ab67de0-1234-5678-9abc-def012345678"
        (projects / f"{sid}.jsonl").write_text(
            '{"type":"system_init","session_id":"' + sid + '"}\n',
            encoding="utf-8",
        )

        persisted: list[tuple[str, str]] = []

        async def fake_persist(engagement_id: str, session_id: str) -> None:
            persisted.append((engagement_id, session_id))

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="x",
            persist_session_id=fake_persist,
        )

        task = asyncio.create_task(drv._capture_session_id(rec))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        session_file = ws / ".session_id"
        assert session_file.exists(), (
            f".session_id file must be written to workspace dir "
            f"({ws}); contents of dir: {list(ws.iterdir())}"
        )
        assert session_file.read_text(encoding="utf-8").strip() == sid
        assert persisted == [(rec.id, sid)], (
            f"persist_session_id callback must be invoked exactly once "
            f"with (engagement_id, session_id); got {persisted!r}"
        )

    async def test_waits_for_projects_dir_to_appear(self, tmp_path):
        """Projects dir does not exist at watcher start (claude CLI has
        not spawned yet — there is a small window between s6 starting
        the service and the CLI writing its first jsonl). Watcher polls
        until the directory + file appear.
        """
        from drivers.claude_code_driver import ClaudeCodeDriver

        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        projects = self._projects_dir(ws)
        # Don't create projects yet — let the watcher poll.

        persisted: list[tuple[str, str]] = []

        async def fake_persist(engagement_id: str, session_id: str) -> None:
            persisted.append((engagement_id, session_id))

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="x",
            persist_session_id=fake_persist,
        )

        task = asyncio.create_task(drv._capture_session_id(rec))

        # Claude CLI starts up after 0.2s and writes its jsonl.
        await asyncio.sleep(0.2)
        projects.mkdir(parents=True)
        sid = "11111111-2222-3333-4444-555555555555"
        (projects / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

        # Give the poller a beat to notice.
        await asyncio.sleep(0.4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert (ws / ".session_id").read_text(encoding="utf-8").strip() == sid
        assert persisted == [(rec.id, sid)]

    async def test_ignores_non_uuid_filenames(self, tmp_path):
        """Watcher must only accept UUID-shaped filenames (claude CLI's
        session files). Other files in the projects dir (logs, locks,
        partial writes) must NOT be persisted as session_ids."""
        from drivers.claude_code_driver import ClaudeCodeDriver

        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        projects = self._projects_dir(ws)
        projects.mkdir(parents=True)
        # Decoy files that look superficially like jsonl but are NOT
        # valid UUIDs. The watcher must skip these.
        (projects / "log.jsonl").write_text("{}\n", encoding="utf-8")
        (projects / "lockfile.jsonl").write_text("{}\n", encoding="utf-8")

        persisted: list[tuple[str, str]] = []

        async def fake_persist(engagement_id: str, session_id: str) -> None:
            persisted.append((engagement_id, session_id))

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="x",
            persist_session_id=fake_persist,
        )

        task = asyncio.create_task(drv._capture_session_id(rec))
        await asyncio.sleep(0.3)

        # Now drop in a real UUID-named file.
        sid = "abcdef00-0000-0000-0000-000000000000"
        (projects / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # The decoy files were ignored; the UUID-named file was captured.
        assert (ws / ".session_id").read_text(encoding="utf-8").strip() == sid
        assert persisted == [(rec.id, sid)]

    async def test_atomic_write_via_temp_rename(self, tmp_path):
        """A partial-write crash must NOT leave a half-written
        ``.session_id`` that a subsequent boot-replay would feed to
        ``claude --resume <truncated>``. Verify temp+rename atomicity
        (no leftover ``.session_id.tmp`` in workspace)."""
        from drivers.claude_code_driver import ClaudeCodeDriver

        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        projects = self._projects_dir(ws)
        projects.mkdir(parents=True)
        sid = "deadbeef-0000-0000-0000-000000000000"
        (projects / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="x",
            persist_session_id=None,  # None tolerated — no registry hook in test
        )

        task = asyncio.create_task(drv._capture_session_id(rec))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        leftovers = [p.name for p in ws.iterdir() if p.name.startswith(".session_id")]
        assert ".session_id" in leftovers
        tmp_leftovers = [n for n in leftovers if n != ".session_id"]
        assert tmp_leftovers == [], (
            f"atomic-write temp file leaked into workspace: {tmp_leftovers!r}"
        )


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
            engagements_root=str(tmp_path),
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
        (tmp_path / "svc" / "engagement-abc12345def67890-log").mkdir()
        (tmp_path / "svc" / "engagement-abc12345def67890-log" / "type").write_text("longrun\n")

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "eng"),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        await drv.cancel(rec)

        # v0.64.0: the sibling logger service is stopped explicitly too, so
        # the follow-up recompile never has to down a still-live service.
        assert stopped == [rec.id, f"{rec.id}-log"]
        assert not (tmp_path / "svc" / f"engagement-{rec.id}").exists()
        assert not (tmp_path / "svc" / f"engagement-{rec.id}-log").exists()

    async def test_cancel_skips_logger_stop_for_legacy_engagement(
        self, monkeypatch, tmp_path,
    ):
        """Engagements created pre-v0.64.0 have no logger service — cancel
        must not exec a doomed `s6-rc -d change engagement-<id>-log`."""
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
        # No engagement-<id>-log sibling (legacy layout).

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "eng"),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        await drv.cancel(rec)

        assert stopped == [rec.id]


class TestRelayLogLines:
    """G5 — claude_code driver relays its per-engagement s6-log lines
    into Casa's logger at DEBUG, on the same `subprocess_cli` logger
    used by Bug 4's stderr callback."""

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="_tail_file uses path semantics that don't work cleanly on Windows",
    )
    async def test_relay_log_lines_emits_debug_per_line(
        self, tmp_path, caplog,
    ):
        import asyncio
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        rec = _make_record()  # id="abc12345def67890"
        log_file = tmp_path / "log-current"

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://x",
        )

        with caplog.at_level(logging.DEBUG, logger="subprocess_cli"):
            task = asyncio.create_task(
                drv._relay_log_lines(rec, log_path=str(log_file)),
            )
            # Lines are written AFTER the relay starts (the real flow: s6-log
            # creates the file once the fresh engagement's CLI first writes).
            await asyncio.sleep(0.2)
            log_file.write_text(
                "first line\n"
                "second line\n"
                "third line https://example/123\n",
                encoding="utf-8",
            )
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        recs = [r for r in caplog.records if r.name == "subprocess_cli"]
        msgs = [r.getMessage() for r in recs]
        assert any("first line" in m for m in msgs), msgs
        assert any("second line" in m for m in msgs), msgs
        assert any("third line" in m for m in msgs), msgs
        # Every relayed record carries engagement_id (first 8 chars of rec.id)
        for r in recs:
            assert getattr(r, "engagement_id", None) == "abc12345", (
                f"missing engagement_id on relay record: {r.getMessage()}"
            )
        assert all(r.levelno == logging.DEBUG for r in recs), (
            "relay must emit DEBUG, not INFO"
        )

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="_tail_file uses path semantics that don't work cleanly on Windows",
    )
    async def test_relay_starts_at_end_of_preexisting_file(
        self, tmp_path, caplog,
    ):
        """Boot replay re-spawns the relay against a file that may already
        hold up to 1 MB of history — re-relaying it would bury fresh lines
        at DEBUG. A pre-existing file is tailed from its end."""
        import asyncio
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        rec = _make_record()
        log_file = tmp_path / "log-current"
        log_file.write_text("old historical line\n", encoding="utf-8")

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path / "engagements"),
            send_to_topic=AsyncMock(),
            casa_framework_mcp_url="http://x",
        )

        with caplog.at_level(logging.DEBUG, logger="subprocess_cli"):
            task = asyncio.create_task(
                drv._relay_log_lines(rec, log_path=str(log_file)),
            )
            await asyncio.sleep(0.3)
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write("fresh line\n")
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        msgs = [
            r.getMessage() for r in caplog.records
            if r.name == "subprocess_cli"
        ]
        assert any("fresh line" in m for m in msgs), msgs
        assert not any("old historical line" in m for m in msgs), msgs


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="mkfifo Linux-only")
class TestWriteToFifoBounded:
    """M13: _write_to_fifo must never park a pooled thread forever when no
    FIFO reader exists — it opens/writes non-blocking with a bounded deadline."""

    def _driver(self, tmp_path, sent):
        from drivers.claude_code_driver import ClaudeCodeDriver

        async def send(topic_id, text):
            sent.append((topic_id, text))
        return ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=send, casa_framework_mcp_url="http://unused",
        )

    async def test_no_reader_returns_within_deadline_and_notifies(self, tmp_path):
        import os
        from types import SimpleNamespace

        ws = tmp_path / "eng-no-reader"
        ws.mkdir()
        os.mkfifo(str(ws / "stdin.fifo"))
        sent = []
        driver = self._driver(tmp_path, sent)
        rec = SimpleNamespace(id="eng-no-reader", topic_id=42)
        # Pre-fix: parks a pool thread forever in open() -> wait_for raises
        # TimeoutError and this FAILS. Fixed: returns within ~1s + notifies.
        await asyncio.wait_for(
            driver._write_to_fifo(rec, "hello", timeout_s=1.0, poll_s=0.05),
            timeout=5.0,
        )
        assert sent and sent[0][0] == 42
        assert rec.id not in driver._last_turn_ts

    async def test_with_reader_delivers_text(self, tmp_path):
        import os
        import threading
        from types import SimpleNamespace

        ws = tmp_path / "eng-reader"
        ws.mkdir()
        fifo = str(ws / "stdin.fifo")
        os.mkfifo(fifo)
        got = []
        t = threading.Thread(
            target=lambda: got.append(
                open(fifo, "r", encoding="utf-8").readline()),
        )
        t.start()
        sent = []
        driver = self._driver(tmp_path, sent)
        rec = SimpleNamespace(id="eng-reader", topic_id=7)
        await asyncio.wait_for(
            driver._write_to_fifo(rec, "hi there", timeout_s=5.0), timeout=5.0,
        )
        t.join(timeout=5.0)
        assert got == ["hi there\n"]
        assert sent == []
        assert rec.id in driver._last_turn_ts


# ---------------------------------------------------------------------------
# W1/Sol B8 — spawn-keyed one-turn inbound queue.
# ---------------------------------------------------------------------------


class _FakeWriter:
    """Injectable ``write_fifo`` for _InboundQueue — records calls, returns a
    mutable ``result`` (True = whole line written)."""

    def __init__(self, result: bool = True):
        self.result = result
        self.calls: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.calls.append(text)
        return self.result


def _async_recorder(store: list):
    async def _fn(text: str) -> None:
        store.append(text)
    return _fn


class _FakeRegistry:
    """Fake engagement registry WITH advance_interaction_state (Task-7 contract
    pinned now)."""

    def __init__(self):
        self.advances: list[tuple[str, str]] = []

    async def advance_interaction_state(self, eng_id: str, kind: str) -> None:
        self.advances.append((eng_id, kind))


def _make_queue(tmp_path, *, writer=None, notices=None, registry=None):
    from drivers.claude_code_driver import _InboundQueue

    writer = writer if writer is not None else _FakeWriter(True)
    notices = notices if notices is not None else []
    return _InboundQueue(
        engagement_id="eng1",
        marker_path=str(tmp_path / ".inbound_pending"),
        write_fifo=writer,
        post_notice=_async_recorder(notices),
        registry=registry,
    )


class TestInboundQueue:
    async def test_message_then_spawn_delivers(self, tmp_path):
        writer = _FakeWriter(True)
        q = _make_queue(tmp_path, writer=writer)
        await q.enqueue("hello")          # reader_ready False → queued only
        assert writer.calls == []
        await q.on_spawn()                # arm → deliver
        assert writer.calls == ["hello"]
        assert q.reader_ready is False    # disarmed after one message

    async def test_spawn_then_message_delivers_no_deadlock(self, tmp_path):
        # Edge-triggered pump (r2-B6): a spawn arms the reader while the queue
        # is empty; a LATER enqueue must deliver immediately — never wait for
        # another spawn that would never come without input.
        writer = _FakeWriter(True)
        q = _make_queue(tmp_path, writer=writer)
        await q.on_spawn()                # arm, empty queue → nothing yet
        assert writer.calls == []
        assert q.reader_ready is True
        await q.enqueue("later")          # delivered immediately
        assert writer.calls == ["later"]

    async def test_one_message_per_spawn(self, tmp_path):
        writer = _FakeWriter(True)
        q = _make_queue(tmp_path, writer=writer)
        await q.enqueue("a")
        await q.enqueue("b")
        await q.on_spawn()
        assert writer.calls == ["a"]      # exactly one per spawn
        await q.on_spawn()
        assert writer.calls == ["a", "b"]

    async def test_failed_write_retains_item(self, tmp_path):
        writer = _FakeWriter(False)       # no reader — write fails
        q = _make_queue(tmp_path, writer=writer)
        await q.enqueue("a")
        await q.on_spawn()
        assert writer.calls == ["a"]      # attempted
        assert len(q._queue) == 1         # retained, not dropped
        # Redelivered on the next spawn once the write can succeed.
        writer.result = True
        await q.on_spawn()
        assert writer.calls == ["a", "a"]
        assert len(q._queue) == 0

    async def test_overflow_notice_at_depth_10(self, tmp_path):
        writer = _FakeWriter(True)
        notices: list[str] = []
        q = _make_queue(tmp_path, writer=writer, notices=notices)
        for i in range(10):               # reader unarmed → all queued
            await q.enqueue(f"m{i}")
        assert len(q._queue) == 10
        assert notices == []
        await q.enqueue("overflow")       # 11th → dropped with one notice
        assert len(q._queue) == 10
        assert len(notices) == 1
        assert "10" in notices[0]

    async def test_initial_prompt_through_queue_no_state_transition(self, tmp_path):
        writer = _FakeWriter(True)
        reg = _FakeRegistry()
        q = _make_queue(tmp_path, writer=writer, registry=reg)
        await q.enqueue("system prompt", is_initial=True)
        await q.on_spawn()
        assert writer.calls == ["system prompt"]
        # The initial prompt must NOT advance interaction state (r2-B7).
        assert reg.advances == []

    async def test_ordinary_message_advances_interaction_state(self, tmp_path):
        writer = _FakeWriter(True)
        reg = _FakeRegistry()
        q = _make_queue(tmp_path, writer=writer, registry=reg)
        await q.enqueue("operator says hi")
        await q.on_spawn()
        assert writer.calls == ["operator says hi"]
        assert reg.advances == [("eng1", "operator_turn")]

    async def test_marker_tracks_pending_depth(self, tmp_path):
        writer = _FakeWriter(True)
        q = _make_queue(tmp_path, writer=writer)
        marker = tmp_path / ".inbound_pending"
        await q.enqueue("a")
        assert marker.read_text().strip() == "1"
        await q.on_spawn()
        assert marker.read_text().strip() == "0"


class TestSpawnBackgroundTasksInbound:
    async def test_relay_task_always_spawned(self, tmp_path):
        """The always-on TopicStreamRelay task is registered regardless of
        LOG_LEVEL (it is the operator's live window, not a debug aid)."""
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        lg = logging.getLogger("subprocess_cli")
        old = lg.level
        lg.setLevel(logging.WARNING)          # DEBUG raw-log relay OFF
        try:
            drv._spawn_background_tasks(rec)
            tasks = drv._tasks[rec.id]
            names = [t.get_name() for t in tasks]
            assert any(n.startswith("topic_relay:") for n in names), names
            assert rec.id in drv._inbound       # queue wired
        finally:
            lg.setLevel(old)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_marker_and_boot_reconcile_in_spawn_background_tasks(
        self, tmp_path,
    ):
        """Sol r2-B6: boot replay calls _spawn_background_tasks DIRECTLY. A
        surviving non-zero .inbound_pending posts a one-time reconcile notice
        and is zeroed."""
        from drivers.claude_code_driver import ClaudeCodeDriver

        sender = AsyncMock()
        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=sender, casa_framework_mcp_url="x",
        )
        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        (ws / ".inbound_pending").write_text("2\n", encoding="utf-8")
        try:
            drv._spawn_background_tasks(rec)
            await asyncio.sleep(0.1)            # let the notice task run
            assert sender.await_count == 1
            posted = sender.await_args[0]
            assert posted[0] == rec.topic_id
            assert "2" in posted[1]
            assert (ws / ".inbound_pending").read_text().strip() == "0"
        finally:
            for t in drv._tasks.get(rec.id, []):
                t.cancel()
            await asyncio.gather(
                *drv._tasks.get(rec.id, []), return_exceptions=True)


class TestAbnormalExitCorrelation:
    async def test_abnormal_exit_correlates_epoch_stderr(self, tmp_path, caplog):
        """r5-B2: spawn(1) then spawn(2) with no intervening result → the
        driver reads the UNIQUE .stderr.1.log and WARNs its tail."""
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        (ws / ".stderr.1.log").write_text(
            "traceback: boom on epoch 1\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            await drv._on_stream_event(rec, "spawn", {"epoch": 1})
            await drv._on_stream_event(rec, "spawn", {"epoch": 2})

        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert any("boom on epoch 1" in m for m in warnings), warnings
        assert any("epoch 1" in m and "abnormal" in m for m in warnings)

    async def test_abnormal_exit_pruned_epoch_diagnostics_unavailable(
        self, tmp_path, caplog,
    ):
        """r5-B2: an abnormal-exit lookup for an epoch whose .stderr.<e>.log
        was pruned → 'diagnostics unavailable', never misattributed."""
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        ws = tmp_path / rec.id
        ws.mkdir()
        # No .stderr.1.log — epoch 1 was pruned after advancing >= 4 epochs.
        with caplog.at_level(logging.WARNING):
            await drv._on_stream_event(rec, "spawn", {"epoch": 1})
            await drv._on_stream_event(rec, "spawn", {"epoch": 5})

        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert any("diagnostics unavailable" in m for m in warnings), warnings

    async def test_result_clears_abnormal_flag(self, tmp_path, caplog):
        """A normal result between spawns clears the pending epoch — the next
        spawn is NOT flagged abnormal."""
        import logging
        from drivers.claude_code_driver import ClaudeCodeDriver

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        (tmp_path / rec.id).mkdir()
        with caplog.at_level(logging.WARNING):
            await drv._on_stream_event(rec, "spawn", {"epoch": 1})
            await drv._on_stream_event(rec, "result", {"subtype": "success"})
            await drv._on_stream_event(rec, "spawn", {"epoch": 2})
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert not any("abnormal" in m for m in warnings), warnings


class TestCancelBypassesQueue:
    async def test_cancel_immediate_while_busy(self, monkeypatch, tmp_path):
        """/cancel drops any messages still waiting for a spawn — it never
        flushes the inbound queue."""
        from drivers.claude_code_driver import ClaudeCodeDriver
        from drivers import s6_rc

        async def fake_stop(*, engagement_id):
            pass
        async def fake_cau():
            pass
        monkeypatch.setattr(s6_rc, "stop_service", fake_stop)
        monkeypatch.setattr(s6_rc, "_compile_and_update_locked", fake_cau)
        monkeypatch.setattr(
            s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(tmp_path / "svc"))
        (tmp_path / "svc").mkdir()

        drv = ClaudeCodeDriver(
            engagements_root=str(tmp_path),
            send_to_topic=AsyncMock(), casa_framework_mcp_url="x",
        )
        rec = _make_record()
        (tmp_path / rec.id).mkdir()

        # Wire the queue; enqueue while unarmed (busy — no spawn yet).
        drv._spawn_background_tasks(rec)
        writer = _FakeWriter(True)
        drv._inbound[rec.id]._write_fifo = writer      # observe FIFO writes
        await drv.send_user_turn(rec, "queued but never delivered")
        assert len(drv._inbound[rec.id]._queue) == 1

        await drv.cancel(rec)

        # Queue state torn down; the message was NEVER written to the FIFO.
        assert rec.id not in drv._inbound
        assert rec.id not in drv._reply_texts
        assert rec.id not in drv._epoch_pending
        assert writer.calls == []
