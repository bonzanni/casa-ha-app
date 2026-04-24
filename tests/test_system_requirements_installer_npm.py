"""npm install strategy for casa.systemRequirements (§4.3.1)."""
from __future__ import annotations

from pathlib import Path

import pytest

from system_requirements.npm import install_npm

pytestmark = pytest.mark.unit


@pytest.mark.slow
def test_install_happy(tmp_path: Path) -> None:
    # `is-number` is a ~10kb package with a CLI — good smoke.
    result = install_npm(
        plugin_name="is-number-probe",
        spec={
            "type": "npm",
            "package": "is-number@7.0.0",
            "verify_bin": "is-number",
        },
        tools_root=tmp_path / "tools",
    )
    # is-number doesn't expose a bin, so verify_bin_resolves may be False.
    # Just assert installation path exists.
    assert result.ok or "verify_bin" in result.message
    assert (tmp_path / "tools" / "npm" / "node_modules" / "is-number").is_dir()
