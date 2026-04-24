"""venv install strategy for casa.systemRequirements (§4.3.1)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from system_requirements.venv import install_venv

pytestmark = pytest.mark.unit


@pytest.mark.slow
def test_install_happy(tmp_path: Path) -> None:
    # Pick a tiny PyPI package that's stable + small (pip itself).
    result = install_venv(
        plugin_name="pip-probe",
        spec={
            "type": "venv",
            "package": "pip==24.0",
            "python": sys.executable,
            "verify_bin": "pip",
        },
        tools_root=tmp_path / "tools",
    )
    assert result.ok
    assert (tmp_path / "tools" / "bin" / "pip").is_symlink()
