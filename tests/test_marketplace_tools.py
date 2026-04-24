"""Configurator MCP tools: marketplace_{add,remove,update,list}_plugin."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def user_mkt(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "marketplace" / ".claude-plugin" / "marketplace.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "name": "casa-plugins", "owner": {"name": "t"}, "plugins": [],
    }), encoding="utf-8")
    monkeypatch.setattr("marketplace_ops.USER_MARKETPLACE_PATH", target)
    return target


@patch("tools.subprocess.run")
def test_marketplace_add_plugin_happy(mock_run, user_mkt) -> None:
    from tools import _tool_marketplace_add_plugin
    mock_run.return_value.returncode = 0
    result = _tool_marketplace_add_plugin(
        plugin_name="face-rec",
        repo_url="https://github.com/u/casa-plugin-face-rec.git",
        ref="abc123",
        description="AWS face ID",
        category="productivity",
    )
    assert result["added"] is True
    data = json.loads(user_mkt.read_text())
    assert data["plugins"][0]["name"] == "face-rec"


def test_marketplace_add_plugin_rejects_apt(user_mkt) -> None:
    from tools import _tool_marketplace_add_plugin
    result = _tool_marketplace_add_plugin(
        plugin_name="ffmpeg-plugin",
        repo_url="https://github.com/u/x.git",
        ref="abc",
        description="x",
        casa_system_requirements=[{"type": "apt", "package": "ffmpeg"}],
    )
    assert "apt" in result["error"]


@patch("tools.subprocess.run")
def test_marketplace_remove_plugin(mock_run, user_mkt) -> None:
    from tools import _tool_marketplace_add_plugin, _tool_marketplace_remove_plugin
    mock_run.return_value.returncode = 0
    _tool_marketplace_add_plugin(
        plugin_name="x", repo_url="https://github.com/u/x.git", ref="a",
        description="d",
    )
    result = _tool_marketplace_remove_plugin(plugin_name="x")
    assert result["removed"] is True


def test_marketplace_list_plugins(user_mkt) -> None:
    from tools import _tool_marketplace_list_plugins
    assert _tool_marketplace_list_plugins() == {"plugins": []}
