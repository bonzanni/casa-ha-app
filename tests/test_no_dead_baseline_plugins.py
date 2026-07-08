"""L30: the dead superpowers v5.0.7 baseline clone and the unused
base_plugins_root parameter (unused since v0.14.x) must stay removed.

Plugin refs are pinned solely in marketplace-defaults/.claude-plugin/
marketplace.json; the Dockerfile must carry neither a shadow clone nor a
shadow SUPERPOWERS_REF pin that can drift from it.
"""

import inspect
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_provision_workspace_has_no_base_plugins_root_param():
    from drivers.workspace import provision_workspace
    assert "base_plugins_root" not in inspect.signature(provision_workspace).parameters


def test_claude_code_driver_has_no_base_plugins_root_param():
    from drivers.claude_code_driver import ClaudeCodeDriver
    assert "base_plugins_root" not in inspect.signature(ClaudeCodeDriver.__init__).parameters


def test_dockerfile_does_not_bake_dead_superpowers_clone():
    text = (REPO_ROOT / "casa-agent" / "Dockerfile").read_text(encoding="utf-8")
    assert "claude-plugins/base" not in text
    assert "SUPERPOWERS_REF" not in text
