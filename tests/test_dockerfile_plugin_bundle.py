"""Dockerfile structural guards for the deterministic bundled-artifact build
(spec 3.6): the build helper runs BEFORE the broad `COPY rootfs /`, and no
marketplace / seed machinery survives."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DOCKERFILE = (Path(__file__).resolve().parent.parent / "casa-agent"
               / "Dockerfile").read_text(encoding="utf-8")


def test_build_helper_runs_before_broad_copy():
    # Match the COMMAND at line-start (not the comment that mentions it).
    build = _DOCKERFILE.find("\nRUN python3 /opt/casa/scripts/build_plugin_bundle.py")
    broad_copy = _DOCKERFILE.find("\nCOPY rootfs /\n")
    assert build != -1 and broad_copy != -1
    assert build < broad_copy, "bundle build must precede COPY rootfs / (cache)"


def test_no_claude_plugin_or_seed_env():
    assert "claude plugin" not in _DOCKERFILE
    assert "CLAUDE_CODE_PLUGIN_SEED_DIR" not in _DOCKERFILE
    assert "claude-seed" not in _DOCKERFILE
    assert "marketplace-defaults" not in _DOCKERFILE


def test_bundle_dir_is_read_only():
    assert "chmod -R a-w /opt/casa/plugin-bundle" in _DOCKERFILE
