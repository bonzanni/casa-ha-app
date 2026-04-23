"""Unit tests for drivers.s6_rc — pure s6-rc orchestration."""

from __future__ import annotations

import os
import platform
import stat

import pytest

pytestmark = pytest.mark.asyncio


class TestWriteServiceDir:
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
        if platform.system() != "Windows":
            assert mode & stat.S_IXUSR, "run script must be executable"
        assert (svc_dir / "dependencies.d" / "init-setup-configs").exists()
