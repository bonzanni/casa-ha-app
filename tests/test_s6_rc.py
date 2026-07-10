"""Unit tests for drivers.s6_rc — pure s6-rc orchestration."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _pair(svc_root: Path, eid: str) -> None:
    """Write a healthy producer/consumer engagement pair."""
    from drivers.s6_rc import write_service_dir

    write_service_dir(
        svc_root=str(svc_root), engagement_id=eid,
        run_script="#!/bin/sh\nexec true\n", depends_on=[],
        log_run_script="#!/bin/sh\nexec s6-log /tmp/x\n",
    )


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
    async def test_writes_sibling_logger_service_when_log_script_provided(self, tmp_path):
        """v0.64.0: s6-rc-compile ignores nested log/ subdirs (skarnet docs) —
        a logged service must be TWO sibling top-level services wired
        producer-for/consumer-for. s6-rc auto-adds the producer→consumer
        dependency (verified empirically on s6-rc 0.6.0.0)."""
        from drivers.s6_rc import write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        log_script = (
            "#!/command/with-contenv sh\n"
            "exec s6-log n20 s1000000 /var/log/casa-engagement-abc12345\n"
        )

        write_service_dir(
            svc_root=str(svc_root),
            engagement_id="abc12345",
            run_script="#!/command/with-contenv sh\nexec true\n",
            depends_on=["init-setup-configs"],
            log_run_script=log_script,
        )

        main_dir = svc_root / "engagement-abc12345"
        log_dir = svc_root / "engagement-abc12345-log"
        assert not (main_dir / "log").exists(), \
            "nested log/ is ignored by s6-rc-compile — must not be written"
        assert (main_dir / "producer-for").read_text() == "engagement-abc12345-log\n"
        assert (log_dir / "type").read_text() == "longrun\n"
        assert (log_dir / "run").read_text() == log_script
        mode = os.stat(log_dir / "run").st_mode
        assert mode & stat.S_IXUSR
        assert (log_dir / "consumer-for").read_text() == "engagement-abc12345\n"
        assert (log_dir / "dependencies.d" / "init-setup-configs").exists()

    async def test_no_logger_artifacts_without_log_script(self, tmp_path):
        from drivers.s6_rc import write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        write_service_dir(
            svc_root=str(svc_root), engagement_id="abc12345",
            run_script="#!/bin/sh\nexec true\n", depends_on=[],
        )
        assert not (svc_root / "engagement-abc12345" / "producer-for").exists()
        assert not (svc_root / "engagement-abc12345-log").exists()


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

    async def test_removes_sibling_logger_dir(self, tmp_path):
        from drivers.s6_rc import remove_service_dir, write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        write_service_dir(
            svc_root=str(svc_root), engagement_id="x1",
            run_script="#!/bin/sh\nexec true\n", depends_on=[],
            log_run_script="#!/bin/sh\nexec s6-log /var/log/casa-engagement-x1\n",
        )
        assert (svc_root / "engagement-x1-log").exists()

        remove_service_dir(svc_root=str(svc_root), engagement_id="x1")
        assert not (svc_root / "engagement-x1").exists()
        assert not (svc_root / "engagement-x1-log").exists()

    async def test_remove_continues_past_rmtree_failure(
        self, tmp_path, monkeypatch,
    ):
        """One rmtree failing must not abort the other half's removal —
        a torn pair would otherwise persist (the compile-path prune is the
        backstop, but removal should make progress on its own)."""
        from drivers import s6_rc

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        _pair(svc_root, "x1")

        real_rmtree = s6_rc.shutil.rmtree

        def flaky(path, *args, **kwargs):
            if str(path).endswith("engagement-x1"):
                raise OSError("EBUSY")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(s6_rc.shutil, "rmtree", flaky)
        # Does not raise; the -log half is removed despite the main failing.
        s6_rc.remove_service_dir(svc_root=str(svc_root), engagement_id="x1")
        assert not (svc_root / "engagement-x1-log").exists()
        assert (svc_root / "engagement-x1").exists()


class TestPruneBrokenPairs:
    """v0.64.0: the pair dirs cross-reference each other, so NO write/remove
    ordering is crash-atomic — a torn half (producer-for naming a missing
    service, or a consumer-for whose producer is gone) fails EVERY
    s6-rc-compile, bricking all engagement orchestration. The compile path
    therefore prunes broken halves into a compilable state first."""

    async def test_dangling_producer_for_is_unlinked(self, tmp_path):
        from drivers.s6_rc import _prune_broken_pairs

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        _pair(svc_root, "torn1")
        shutil.rmtree(svc_root / "engagement-torn1-log")

        _prune_broken_pairs(svc_root=str(svc_root))

        # The engagement service survives (unlogged); the dangling
        # cross-reference is gone so the sources compile again.
        assert (svc_root / "engagement-torn1").is_dir()
        assert not (svc_root / "engagement-torn1" / "producer-for").exists()

    async def test_orphan_log_sibling_is_removed(self, tmp_path):
        from drivers.s6_rc import _prune_broken_pairs

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        _pair(svc_root, "torn2")
        shutil.rmtree(svc_root / "engagement-torn2")

        _prune_broken_pairs(svc_root=str(svc_root))

        assert not (svc_root / "engagement-torn2-log").exists()

    async def test_log_sibling_without_producer_for_is_removed(self, tmp_path):
        from drivers.s6_rc import _prune_broken_pairs

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        _pair(svc_root, "torn3")
        (svc_root / "engagement-torn3" / "producer-for").unlink()

        _prune_broken_pairs(svc_root=str(svc_root))

        assert (svc_root / "engagement-torn3").is_dir()
        assert not (svc_root / "engagement-torn3-log").exists()

    async def test_healthy_pair_untouched(self, tmp_path):
        from drivers.s6_rc import _prune_broken_pairs

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        _pair(svc_root, "ok1")

        _prune_broken_pairs(svc_root=str(svc_root))

        assert (svc_root / "engagement-ok1" / "producer-for").exists()
        assert (svc_root / "engagement-ok1-log" / "consumer-for").exists()

    async def test_compile_prunes_before_compiling(self, tmp_path, monkeypatch):
        from drivers import s6_rc

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        _pair(svc_root, "torn4")
        shutil.rmtree(svc_root / "engagement-torn4-log")
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

        calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            calls.append(list(argv))
            class _R: returncode = 0
            return _R()

        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        await s6_rc._compile_and_update_locked()

        assert not (svc_root / "engagement-torn4" / "producer-for").exists()
        assert calls and calls[0][0] == "s6-rc-compile"


class TestStopLogService:
    async def test_stops_when_log_source_dir_exists(self, tmp_path, monkeypatch):
        from drivers import s6_rc

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        (svc_root / "engagement-abc-log").mkdir()
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

        calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            calls.append(list(argv))
            class _R: returncode = 0
            return _R()

        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        await s6_rc.stop_log_service(engagement_id="abc")

        assert calls == [["s6-rc", "-d", "change", "engagement-abc-log"]]

    async def test_noop_when_log_source_dir_absent(self, tmp_path, monkeypatch):
        """Legacy engagements (pre-v0.64.0 layout) have no logger service —
        stopping one would exec a doomed s6-rc and log a spurious warning."""
        from drivers import s6_rc

        svc_root = tmp_path / "svc"
        svc_root.mkdir()
        monkeypatch.setattr(s6_rc, "ENGAGEMENT_SOURCES_ROOT", str(svc_root))

        calls: list[list[str]] = []
        monkeypatch.setattr(
            s6_rc.subprocess, "run",
            lambda argv, check=True, **kw: calls.append(list(argv)),
        )

        await s6_rc.stop_log_service(engagement_id="abc")

        assert calls == []


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

    async def test_reaps_previous_db_after_successful_swap(self, tmp_path, monkeypatch):
        """L12 leak guard: the previously-live compiled db must be removed
        after a successful swap, so /tmp doesn't accumulate one orphan per
        compile."""
        import subprocess

        from drivers import s6_rc

        old_db = tmp_path / "s6-casa-db-old"
        old_db.mkdir()
        live = tmp_path / "compiled"
        live.symlink_to(old_db)
        monkeypatch.setattr(s6_rc, "LIVE_DB_SYMLINK", str(live))

        calls: list[list[str]] = []

        def fake_run(argv, check=True, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        await s6_rc._compile_and_update_locked()

        assert calls[0][0] == "s6-rc-compile"
        assert calls[1][0] == "s6-rc-update"
        assert not old_db.exists(), "previous live db must be reaped after a successful swap"

    async def test_keeps_non_casa_previous_db(self, tmp_path, monkeypatch):
        """A foreign/boot db (no s6-casa-db- prefix) must never be reaped."""
        import subprocess

        from drivers import s6_rc

        boot_db = tmp_path / "db"  # simulates the s6-overlay boot db
        boot_db.mkdir()
        live = tmp_path / "compiled"
        live.symlink_to(boot_db)
        monkeypatch.setattr(s6_rc, "LIVE_DB_SYMLINK", str(live))
        monkeypatch.setattr(
            s6_rc.subprocess, "run",
            lambda argv, check=True, **kwargs: subprocess.CompletedProcess(argv, 0),
        )

        await s6_rc._compile_and_update_locked()

        assert boot_db.exists(), "foreign db must never be touched"

    async def test_reaps_new_db_on_failed_swap(self, tmp_path, monkeypatch):
        """A failed compile/update must reap the just-created db (the orphan
        in that scenario), not the previous live one."""
        import subprocess

        from drivers import s6_rc

        old_db = tmp_path / "s6-casa-db-old"
        old_db.mkdir()
        live = tmp_path / "compiled"
        live.symlink_to(old_db)
        monkeypatch.setattr(s6_rc, "LIVE_DB_SYMLINK", str(live))

        captured_new_db: list[str] = []

        def fake_run(argv, check=True, **kwargs):
            if argv[0] == "s6-rc-compile":
                captured_new_db.append(argv[1])
                Path(argv[1]).mkdir(parents=True, exist_ok=True)
            raise subprocess.CalledProcessError(1, argv)

        monkeypatch.setattr(s6_rc.subprocess, "run", fake_run)

        with pytest.raises(subprocess.CalledProcessError):
            await s6_rc._compile_and_update_locked()

        assert old_db.exists(), "previous live db must survive a failed swap"
        assert not Path(captured_new_db[0]).exists(), "failed new db must be reaped"


class TestServiceStatus:
    async def test_service_is_up_parses_svstat(self, monkeypatch):
        from drivers import s6_rc

        def fake_run(argv, **kwargs):
            class _R:
                returncode = 0
                stdout = "12345\n"        # s6-svstat -p prints pid (0 if down)
            assert argv == ["s6-svstat", "-p", "/run/service/engagement-abc"]
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

    async def test_logger_dirs_follow_their_engagement(self, tmp_path):
        """v0.64.0: engagement-<id>-log is kept iff <id> is kept. The
        pre-fix parser read the whole suffix as engagement id '<id>-log'
        (never in the keep set) and swept live loggers at boot."""
        from drivers.s6_rc import sweep_orphan_service_dirs, write_service_dir

        svc_root = tmp_path / "casa-s6-services"
        svc_root.mkdir()
        for eid in ("keep1", "orphan1"):
            write_service_dir(
                svc_root=str(svc_root), engagement_id=eid,
                run_script="#!/bin/sh\nexec true\n", depends_on=[],
                log_run_script="#!/bin/sh\nexec s6-log /tmp/x\n",
            )

        removed = sweep_orphan_service_dirs(
            svc_root=str(svc_root), keep_engagement_ids={"keep1"},
        )

        assert set(removed) == {"orphan1"}, "orphan pair counts once"
        assert (svc_root / "engagement-keep1").exists()
        assert (svc_root / "engagement-keep1-log").exists()
        assert not (svc_root / "engagement-orphan1").exists()
        assert not (svc_root / "engagement-orphan1-log").exists()


class TestSweepOrphanCompiledDbs:
    async def test_removes_stale_dbs_keeps_live_and_foreign(self, tmp_path, monkeypatch):
        from drivers import s6_rc

        keep = tmp_path / "s6-casa-db-live"
        keep.mkdir()
        stale = tmp_path / "s6-casa-db-stale"
        stale.mkdir()
        foreign = tmp_path / "not-ours"
        foreign.mkdir()
        live = tmp_path / "compiled"
        live.symlink_to(keep)
        monkeypatch.setattr(s6_rc, "LIVE_DB_SYMLINK", str(live))

        removed = s6_rc.sweep_orphan_compiled_dbs(tmp_root=str(tmp_path))

        assert removed == [str(stale)]
        assert keep.exists() and foreign.exists() and not stale.exists()

    async def test_missing_tmp_root_is_noop(self, tmp_path):
        from drivers import s6_rc

        missing = tmp_path / "does-not-exist"
        assert s6_rc.sweep_orphan_compiled_dbs(tmp_root=str(missing)) == []
