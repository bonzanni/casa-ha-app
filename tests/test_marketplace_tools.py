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
    # R-6: a github source must pin via `ref` — bundled CC 2.1.150 rejects a
    # `sha` key on a github source as "source type not supported", which broke
    # every install_casa_plugin (Stage 2 `claude plugin install`).
    assert data["plugins"][0]["source"]["ref"] == "abc123"
    assert "sha" not in data["plugins"][0]["source"]


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


def _agent_home_with(tmp_path: Path, monkeypatch, *, enabled_by: dict[str, bool]):
    """Build an agent-home tree; `enabled_by` maps role -> whether it enables greet."""
    home_root = tmp_path / "agent-home"
    for role, enables in enabled_by.items():
        (home_root / role / ".claude").mkdir(parents=True)
        ep = {"greet@casa-plugins": True} if enables else {}
        (home_root / role / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": ep}), encoding="utf-8")
    monkeypatch.setattr("tools._AGENT_HOME_ROOT", home_root)
    cache_root = tmp_path / "cache"
    (cache_root / "greet" / "0.1.0").mkdir(parents=True)
    monkeypatch.setattr("tools._CASA_PLUGIN_CACHE_ROOT", cache_root)
    return cache_root


@patch("tools.subprocess.run")
def test_uninstall_sweeps_cache_when_unenabled(mock_run, tmp_path, monkeypatch) -> None:
    """R-9: once no agent-home still enables the plugin, uninstall sweeps the
    shared marketplace cache dir (claude plugin uninstall leaves it orphaned)."""
    from tools import _tool_uninstall_casa_plugin
    mock_run.return_value.returncode = 0
    cache_root = _agent_home_with(tmp_path, monkeypatch, enabled_by={"assistant": False})

    result = _tool_uninstall_casa_plugin(plugin_name="greet", targets=["assistant"])
    assert result["uninstalled_from"] == ["assistant"]
    assert result["cache_swept"] is True
    assert not (cache_root / "greet").exists()


@patch("tools.subprocess.run")
def test_uninstall_keeps_cache_when_still_enabled(mock_run, tmp_path, monkeypatch) -> None:
    """R-9: the shared cache is kept while another agent-home still enables it."""
    from tools import _tool_uninstall_casa_plugin
    mock_run.return_value.returncode = 0
    cache_root = _agent_home_with(
        tmp_path, monkeypatch, enabled_by={"assistant": False, "butler": True})

    result = _tool_uninstall_casa_plugin(plugin_name="greet", targets=["assistant"])
    assert result["cache_swept"] is False
    assert (cache_root / "greet").exists()


def test_marketplace_list_plugins(user_mkt) -> None:
    from tools import _tool_marketplace_list_plugins
    assert _tool_marketplace_list_plugins() == {"plugins": []}
