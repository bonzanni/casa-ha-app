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


def test_narrow_copy_ships_text_util_for_plugin_store():
    """v0.78.0 W1: plugin_store.py now imports text_util (stdlib-only) at
    module scope for sanitize_segment/is_unsafe_text. The build helper runs
    BEFORE the broad `COPY rootfs /`, so text_util.py must be in the SAME
    narrow COPY as plugin_store.py, or the build-time import fails (only the
    image build catches this — the unit gate runs against the full rootfs
    checkout and can't see a missing narrow COPY)."""
    narrow_copy = _DOCKERFILE.find(
        "\nCOPY rootfs/opt/casa/plugin_registry.py rootfs/opt/casa/plugin_store.py")
    assert narrow_copy != -1
    line_end = _DOCKERFILE.index("\n", narrow_copy + 1)
    line = _DOCKERFILE[narrow_copy:line_end]
    assert "rootfs/opt/casa/text_util.py" in line
