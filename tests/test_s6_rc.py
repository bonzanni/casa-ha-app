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
