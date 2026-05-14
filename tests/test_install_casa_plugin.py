"""Two-stage install: system requirements then per-agent-home plugin install."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def user_mkt(tmp_path: Path, monkeypatch) -> Path:
    """Write a user marketplace with one face-rec entry."""
    target = tmp_path / "marketplace" / ".claude-plugin" / "marketplace.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "name": "casa-plugins", "owner": {"name": "t"},
        "plugins": [
            {"name": "face-rec",
             "description": "x", "version": "0.1.0",
             "source": {"source": "github", "repo": "u/face-rec", "sha": "abc"},
             "category": "productivity"},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr("marketplace_ops.USER_MARKETPLACE_PATH", target)
    # Redirect agent-home mkdir away from real /addon_configs/ (permission-denied on CI).
    monkeypatch.setattr("tools._AGENT_HOME_ROOT", tmp_path / "agent-home")
    return target


@patch("tools.subprocess.run")
def test_install_happy_no_sysreqs(mock_run, user_mkt, tmp_path: Path) -> None:
    from tools import _tool_install_casa_plugin
    # All subprocess.run calls return success
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = ""
    mock_run.return_value.stderr = ""

    result = _tool_install_casa_plugin(
        plugin_name="face-rec",
        targets=["assistant"],
    )
    assert result["ok"] is True
    assert result["installed_on"] == ["assistant"]
    assert result["system_requirements_installed"] == 0


def test_install_plugin_not_in_marketplace(user_mkt) -> None:
    from tools import _tool_install_casa_plugin
    result = _tool_install_casa_plugin(
        plugin_name="does-not-exist",
        targets=["assistant"],
    )
    assert result["ok"] is False
    assert result["error"] == "plugin_not_in_marketplace"


async def test_install_plugin_failure_envelope_is_error(user_mkt) -> None:
    """O-1 (v0.37.9): `install_casa_plugin` returns `{ok: False, ...}` on
    failure; the MCP envelope must set ``is_error: True`` so
    ``sdk_logging.log_tool_result`` emits ``ok=False`` in turn telemetry.

    Pre-v0.37.9 `_result()` only auto-detected `status == "error"`, so the
    `{ok: False, ...}` shape used by install/uninstall paths landed as
    `ok=True` in operator-visible logs — contradicting F-7 v0.32.0 intent.
    Live evidence: 2026-05-14 P29.1 cid `52240634` saw
    `tool_result idx=10 name=mcp__casa-framework__install_casa_plugin
    ok=True ms=12594` despite the payload being a plugin-not-in-marketplace
    failure.
    """
    from tools import install_casa_plugin
    envelope = await install_casa_plugin.handler({
        "plugin_name": "does-not-exist",
        "targets": ["assistant"],
    })
    assert envelope.get("is_error") is True, (
        f"install_casa_plugin failure must set is_error=True on the "
        f"MCP envelope so sdk_logging.log_tool_result emits ok=False. "
        f"envelope keys: {sorted(envelope.keys())}"
    )
    payload = json.loads(envelope["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["error"] == "plugin_not_in_marketplace"


@patch("tools.subprocess.run")
def test_install_stage2_failure_triggers_rollback(mock_run, user_mkt, tmp_path: Path, monkeypatch) -> None:
    """When agent-home install fails AND there are system-requirements outcomes,
    stage-1 install dirs get rmtree'd."""
    from tools import _tool_install_casa_plugin
    from system_requirements.orchestrator import RequirementOutcome

    # Make subprocess.run return:
    #   - 0 for "claude plugin marketplace update"
    #   - non-zero for "flock ... claude plugin install"
    call_count = [0]
    def side_effect(*args, **kwargs):
        call_count[0] += 1
        ret = MagicMock()
        cmd = args[0] if args else kwargs.get("args", [])
        if "flock" in cmd:
            ret.returncode = 1
            ret.stdout = ""
            ret.stderr = "install failed"
        else:
            ret.returncode = 0
            ret.stdout = ""
            ret.stderr = ""
        return ret
    mock_run.side_effect = side_effect

    # Seed marketplace entry with systemRequirements so stage-1 produces outcomes
    data = json.loads((user_mkt).read_text())
    data["plugins"][0]["casa"] = {"systemRequirements": [
        {"type": "tarball", "url": "http://x", "sha256": "a"*64, "verify_bin": "b"}
    ]}
    user_mkt.write_text(json.dumps(data), encoding="utf-8")

    # Mock install_requirements to produce a fake outcome we can verify rollback against.
    fake_install_dir = tmp_path / "tools" / "face-rec-0.1.0"
    fake_install_dir.mkdir(parents=True)
    fake_outcome = RequirementOutcome(
        requirement={"type": "tarball", "verify_bin": "b"},
        winning_strategy="tarball",
        install_dir=fake_install_dir,
        verify_bin="b",
    )
    with patch("tools.install_requirements", return_value=[fake_outcome]):
        with patch("tools.add_manifest"):  # bypass manifest write
            result = _tool_install_casa_plugin(
                plugin_name="face-rec",
                targets=["assistant"],
            )

    assert result["ok"] is False
    assert result["error"] == "agent_install_failed"
    # Rollback: install dir should be gone
    assert not fake_install_dir.exists(), "stage-1 install dir should be rolled back"
