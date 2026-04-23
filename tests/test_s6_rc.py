"""Unit tests for drivers.s6_rc — pure s6-rc orchestration."""

from __future__ import annotations

import os
import stat
import sys

import pytest

pytestmark = pytest.mark.asyncio


class TestWriteServiceDir:
    @pytest.mark.skipif(sys.platform == "win32", reason="chmod exec-bits not meaningful on Windows")
    async def test_writes_type_run_and_dependencies(self, tmp_path):
        from drivers.s6_rc import write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        run_contents = "#!/command/with-contenv sh\nexec true\n"

        write_service_dir(
            svc_root=str(svc_root),
            engagement_id="abc12345",
            run_script=run_contents,
            depends_on=["init-setup-configs"],
        )

        svc_dir = svc_root / "engagement-abc12345"
        assert (svc_dir / "type").read_text() == "longrun\n"
        assert (svc_dir / "run").read_text() == run_contents
        mode = os.stat(svc_dir / "run").st_mode
        assert mode & stat.S_IXUSR, "run script must be executable"
        assert (svc_dir / "dependencies.d" / "init-setup-configs").exists()


    @pytest.mark.skipif(sys.platform == "win32", reason="chmod exec-bits not meaningful on Windows")
    async def test_writes_log_subservice_when_log_script_provided(self, tmp_path):
        from drivers.s6_rc import write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()

        write_service_dir(
            svc_root=str(svc_root),
            engagement_id="abc12345",
            run_script="#!/command/with-contenv sh\nexec true\n",
            depends_on=[],
            log_run_script="#!/command/with-contenv sh\nexec s6-log /var/log/casa-engagement-abc12345/\n",
        )

        log_dir = svc_root / "engagement-abc12345" / "log"
        assert log_dir.is_dir()
        assert (log_dir / "type").read_text() == "longrun\n"
        assert (log_dir / "run").is_file()
        mode = os.stat(log_dir / "run").st_mode
        assert mode & stat.S_IXUSR
        assert (log_dir / "dependencies.d" / "engagement-abc12345").exists()


class TestRemoveServiceDir:
    async def test_removes_existing_dir(self, tmp_path):
        from drivers.s6_rc import remove_service_dir, write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        write_service_dir(
            svc_root=str(svc_root), engagement_id="x1",
            run_script="#!/bin/sh\nexec true\n", depends_on=[],
        )
        assert (svc_root / "engagement-x1").exists()

        remove_service_dir(svc_root=str(svc_root), engagement_id="x1")
        assert not (svc_root / "engagement-x1").exists()

    async def test_remove_missing_is_noop(self, tmp_path):
        from drivers.s6_rc import remove_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        # Does not raise.
        remove_service_dir(svc_root=str(svc_root), engagement_id="nosuch")


class TestCompileAndUpdateLocked:
    async def test_invokes_compile_then_update_with_three_sources(self, monkeypatch):
        """The canonical call: compile with overlay + casa + engagement sources, update."""
        from drivers import s6_rc

        calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            calls.append(list(argv))
            class _R: returncode = 0
            return _R()

        # Route both asyncio.to_thread(subprocess.run, ...) and direct subprocess.run
        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        await s6_rc._compile_and_update_locked()

        # Two calls: compile, then update
        assert len(calls) == 2
        compile_cmd = calls[0]
        assert compile_cmd[0] == "s6-rc-compile"
        # Compile receives: new_db, overlay_src, casa_src, engagement_src
        assert compile_cmd[2] == s6_rc.S6_OVERLAY_SOURCES
        assert compile_cmd[3] == s6_rc.CASA_SOURCES
        assert compile_cmd[4] == s6_rc.ENGAGEMENT_SOURCES_ROOT
        new_db = compile_cmd[1]
        assert new_db.startswith("/tmp/s6-casa-db-")

        update_cmd = calls[1]
        assert update_cmd == ["s6-rc-update", new_db]


class TestServiceStatus:
    async def test_service_is_up_parses_svstat(self, monkeypatch):
        from drivers import s6_rc

        def fake_run(argv, **kwargs):
            class _R:
                returncode = 0
                stdout = "12345\n"        # s6-svstat -u prints pid or 0
            assert argv == ["s6-svstat", "-u", "/run/service/engagement-abc"]
            return _R()
        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        pid = await s6_rc.service_pid(engagement_id="abc")
        assert pid == 12345

    async def test_service_is_up_returns_none_when_down(self, monkeypatch):
        from drivers import s6_rc

        def fake_run(argv, **kwargs):
            class _R:
                returncode = 0
                stdout = "0\n"
            return _R()
        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        pid = await s6_rc.service_pid(engagement_id="down")
        assert pid is None


class TestStartStopService:
    async def test_start_service_invokes_rc_change(self, monkeypatch):
        from drivers import s6_rc
        calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            calls.append(list(argv))
            class _R: returncode = 0
            return _R()
        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        await s6_rc.start_service(engagement_id="abc")

        assert calls == [["s6-rc", "-u", "change", "engagement-abc"]]

    async def test_stop_service_invokes_rc_change_down(self, monkeypatch):
        from drivers import s6_rc
        calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            calls.append(list(argv))
            class _R: returncode = 0
            return _R()
        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        await s6_rc.stop_service(engagement_id="abc")

        assert calls == [["s6-rc", "-d", "change", "engagement-abc"]]


class TestSweepOrphans:
    async def test_removes_dirs_not_in_keep_set(self, tmp_path):
        from drivers.s6_rc import sweep_orphan_service_dirs, write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()

        for eid in ("keep1", "keep2", "orphan1", "orphan2"):
            write_service_dir(
                svc_root=str(svc_root), engagement_id=eid,
                run_script="#!/bin/sh\nexec true\n", depends_on=[],
            )

        removed = sweep_orphan_service_dirs(
            svc_root=str(svc_root), keep_engagement_ids={"keep1", "keep2"},
        )

        assert set(removed) == {"orphan1", "orphan2"}
        assert (svc_root / "engagement-keep1").exists()
        assert (svc_root / "engagement-keep2").exists()
        assert not (svc_root / "engagement-orphan1").exists()
        assert not (svc_root / "engagement-orphan2").exists()

    async def test_ignores_non_engagement_dirs(self, tmp_path):
        """A foreign dir under svc_root is left alone (defensive)."""
        from drivers.s6_rc import sweep_orphan_service_dirs

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        (svc_root / "random-other-thing").mkdir()

        removed = sweep_orphan_service_dirs(
            svc_root=str(svc_root), keep_engagement_ids=set(),
        )
        assert removed == []
        assert (svc_root / "random-other-thing").exists()
