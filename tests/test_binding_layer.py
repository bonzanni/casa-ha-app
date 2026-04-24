"""Casa-side binding layer — reads `claude plugin list --json` output,
translates to SDK `plugins=[{type, path}]` shape."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from plugins_binding import build_sdk_plugins, BindingError

pytestmark = pytest.mark.unit


SAMPLE_LIST_JSON = json.dumps([
    {
        "id": "superpowers@casa-plugins-defaults",
        "version": "5.0.7",
        "scope": "user",
        "enabled": True,
        "installPath": "/opt/claude-seed/cache/casa-plugins-defaults/superpowers/5.0.7",
        "installedAt": "2026-04-24T00:00:00Z",
        "lastUpdated": "2026-04-24T00:00:00Z",
    },
    {
        "id": "face-rec@casa-plugins",
        "version": "0.1.0",
        "scope": "user",
        "enabled": True,
        "installPath": "/addon_configs/casa-agent/cc-home/.claude/plugins/cache/casa-plugins/face-rec/0.1.0",
        "installedAt": "2026-04-24T00:00:00Z",
        "lastUpdated": "2026-04-24T00:00:00Z",
    },
    {
        "id": "disabled@casa-plugins",
        "version": "0.0.1",
        "scope": "user",
        "enabled": False,
        "installPath": "/addon_configs/casa-agent/cc-home/.claude/plugins/cache/casa-plugins/disabled/0.0.1",
        "installedAt": "2026-04-24T00:00:00Z",
        "lastUpdated": "2026-04-24T00:00:00Z",
    },
])


@patch("plugins_binding.subprocess.run")
def test_build_sdk_plugins_happy_path(mock_run, tmp_path: Path) -> None:
    mock_run.return_value.stdout = SAMPLE_LIST_JSON
    mock_run.return_value.returncode = 0

    plugins = build_sdk_plugins(
        home=tmp_path / "home",
        shared_cache=tmp_path / "cache",
        seed=tmp_path / "seed",
    )

    assert plugins == [
        {"type": "local", "path": "/opt/claude-seed/cache/casa-plugins-defaults/superpowers/5.0.7"},
        {"type": "local", "path": "/addon_configs/casa-agent/cc-home/.claude/plugins/cache/casa-plugins/face-rec/0.1.0"},
    ]
    # env passed to subprocess.run includes HOME / CACHE_DIR / SEED_DIR
    env = mock_run.call_args.kwargs["env"]
    assert env["HOME"] == str(tmp_path / "home")
    assert env["CLAUDE_CODE_PLUGIN_CACHE_DIR"] == str(tmp_path / "cache")
    assert env["CLAUDE_CODE_PLUGIN_SEED_DIR"] == str(tmp_path / "seed")


@patch("plugins_binding.subprocess.run")
def test_build_sdk_plugins_empty(mock_run, tmp_path: Path) -> None:
    mock_run.return_value.stdout = "[]"
    mock_run.return_value.returncode = 0
    assert build_sdk_plugins(home=tmp_path, shared_cache=tmp_path, seed=tmp_path) == []


@patch("plugins_binding.subprocess.run")
def test_build_sdk_plugins_malformed_json(mock_run, tmp_path: Path) -> None:
    mock_run.return_value.stdout = "not json"
    mock_run.return_value.returncode = 0
    with pytest.raises(BindingError):
        build_sdk_plugins(home=tmp_path, shared_cache=tmp_path, seed=tmp_path)


@patch("plugins_binding.subprocess.run")
def test_build_sdk_plugins_cli_failure_returns_empty(mock_run, tmp_path: Path, caplog) -> None:
    """CLI failure logs a warning and returns empty list — agents still boot,
    they just lack plugin-provided capabilities until the next rebuild."""
    import subprocess as sp
    mock_run.side_effect = sp.CalledProcessError(1, "claude", stderr="boom")
    result = build_sdk_plugins(home=tmp_path, shared_cache=tmp_path, seed=tmp_path)
    assert result == []
    assert any("binding layer degraded" in rec.message for rec in caplog.records)
