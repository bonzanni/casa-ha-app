"""Install_casa_plugin two-stage commit semantics (§4.3.3 / §7.3)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools import _tool_install_casa_plugin

pytestmark = pytest.mark.unit


@pytest.fixture
def user_mkt_with_face_rec(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "marketplace" / ".claude-plugin" / "marketplace.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "name": "casa-plugins", "plugins": [{
            "name": "face-rec", "description": "d", "version": "0.1.0",
            "source": {"source": "github", "repo": "u/x"},
        }],
    }), encoding="utf-8")
    monkeypatch.setattr("marketplace_ops.USER_MARKETPLACE_PATH", target)
    return target


def test_stage1_failure_no_agent_home_writes(tmp_path: Path, monkeypatch, user_mkt_with_face_rec) -> None:
    """When systemRequirements install fails, NO agent-home is touched."""
    # Patch marketplace entry to declare a failing systemRequirement.
    data = json.loads(user_mkt_with_face_rec.read_text())
    data["plugins"][0]["casa"] = {"systemRequirements": [{
        "type": "tarball", "url": "http://nonexistent/bad.tar.gz",
        "sha256": "0" * 64, "verify_bin": "fakebin",
    }]}
    user_mkt_with_face_rec.write_text(json.dumps(data))

    agent_home_root = tmp_path / "agent-home"

    with patch("tools.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        # Make install_requirements raise OrchestrationError to trigger stage 1 failure
        from system_requirements.orchestrator import OrchestrationError
        with patch("tools.install_requirements", side_effect=OrchestrationError("fake failure")):
            result = _tool_install_casa_plugin(plugin_name="face-rec", targets=["ellen"])

    assert result["ok"] is False
    assert "system_requirements_failed" in result.get("error", "")
    # No agent-home writes.
    assert not (agent_home_root / "ellen").exists()
