"""verify_plugin_state tool: tools / secrets / mcp readiness."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def plugin_layout(tmp_path: Path, monkeypatch) -> dict:
    """Set up a fake manifest, plugin cache, and env-conf layout."""
    manifest_path = tmp_path / "system-requirements.yaml"
    monkeypatch.setattr("system_requirements.manifest.MANIFEST_PATH", manifest_path)

    env_conf = tmp_path / "plugin-env.conf"
    monkeypatch.setattr("plugin_env_conf.PLUGIN_ENV_CONF_PATH", env_conf)

    tools_bin = tmp_path / "tools" / "bin"
    tools_bin.mkdir(parents=True)

    cache_root = tmp_path / "cc-home" / ".claude" / "plugins" / "cache" / "casa-plugins"
    (cache_root / "face-rec" / "0.1.0").mkdir(parents=True)
    mcp_json = cache_root / "face-rec" / "0.1.0" / ".mcp.json"

    return {
        "tools_bin": tools_bin,
        "cache_root": cache_root,
        "mcp_json": mcp_json,
        "env_conf": env_conf,
        "manifest_path": manifest_path,
        "tmp_path": tmp_path,
    }


def test_verify_all_ready(plugin_layout, monkeypatch) -> None:
    from tools import _tool_verify_plugin_state
    from system_requirements.manifest import add_plugin_entry as add_manifest_entry
    from plugin_env_conf import set_entry

    # Write manifest entry, create a verify_bin symlink, write mcp.json, set secret.
    add_manifest_entry({
        "name": "face-rec",
        "winning_strategy": "tarball",
        "install_dir": "/t/face-rec-0.1.0",
        "verify_bin": "fakebin",
        "declared_at": "2026-04-24T00:00:00Z",
    })
    (plugin_layout["tools_bin"] / "fakebin").write_text("#!/bin/sh\n")
    plugin_layout["mcp_json"].write_text(json.dumps({
        "mcpServers": {"s": {"command": "x", "env": {"AWS_REGION": "${AWS_REGION}"}}}
    }))
    set_entry("AWS_REGION", "eu-central-1")

    result = _tool_verify_plugin_state(
        plugin_name="face-rec",
        _tools_bin=plugin_layout["tools_bin"],
        _cache_root=plugin_layout["cache_root"],
    )
    assert result["ready"] is True
    assert all(t["status"] == "ready" for t in result["tools"])
    assert all(s["status"] == "resolved" for s in result["secrets"])
    assert result["mcp_started"] is True
    assert result["mcp_errors"] == []


def test_verify_missing_tools(plugin_layout, monkeypatch) -> None:
    from tools import _tool_verify_plugin_state
    from system_requirements.manifest import add_plugin_entry as add_manifest_entry

    add_manifest_entry({
        "name": "missing-plug",
        "winning_strategy": "venv",
        "install_dir": "/t/missing-plug",
        "verify_bin": "nothere",
        "declared_at": "2026-04-24T00:00:00Z",
    })
    # No verify_bin file is placed, so it should be "missing".
    # No mcp.json for this plugin either.
    result = _tool_verify_plugin_state(
        plugin_name="missing-plug",
        _tools_bin=plugin_layout["tools_bin"],
        _cache_root=plugin_layout["cache_root"],
    )
    assert result["ready"] is False
    assert any(t["status"] == "missing" for t in result["tools"])


def test_verify_missing_secret(plugin_layout, monkeypatch) -> None:
    from tools import _tool_verify_plugin_state
    from system_requirements.manifest import add_plugin_entry as add_manifest_entry

    # Plugin has a verify_bin (present) but the required secret is not set.
    add_manifest_entry({
        "name": "face-rec",
        "winning_strategy": "tarball",
        "install_dir": "/t/face-rec-0.1.0",
        "verify_bin": "fakebin",
        "declared_at": "2026-04-24T00:00:00Z",
    })
    (plugin_layout["tools_bin"] / "fakebin").write_text("#!/bin/sh\n")
    plugin_layout["mcp_json"].write_text(json.dumps({
        "mcpServers": {"s": {"command": "x", "env": {"SECRET_KEY": "${SECRET_KEY}"}}}
    }))
    # Deliberately do NOT call set_entry("SECRET_KEY", ...).

    result = _tool_verify_plugin_state(
        plugin_name="face-rec",
        _tools_bin=plugin_layout["tools_bin"],
        _cache_root=plugin_layout["cache_root"],
    )
    assert result["ready"] is False
    assert any(s["status"] == "unresolved" for s in result["secrets"])


def test_verify_plugin_secrets_backcompat(plugin_layout, monkeypatch) -> None:
    from tools import _tool_verify_plugin_secrets
    from system_requirements.manifest import add_plugin_entry as add_manifest_entry
    from plugin_env_conf import set_entry
    from unittest.mock import patch

    add_manifest_entry({
        "name": "face-rec",
        "winning_strategy": "tarball",
        "install_dir": "/t/face-rec-0.1.0",
        "verify_bin": "fakebin",
        "declared_at": "2026-04-24T00:00:00Z",
    })
    (plugin_layout["tools_bin"] / "fakebin").write_text("#!/bin/sh\n")
    plugin_layout["mcp_json"].write_text(json.dumps({
        "mcpServers": {"s": {"command": "x", "env": {"AWS_REGION": "${AWS_REGION}"}}}
    }))
    set_entry("AWS_REGION", "eu-central-1")

    # The back-compat shim calls _tool_verify_plugin_state with production paths.
    # Patch it to forward our test paths.
    import tools as tools_mod

    original = tools_mod._tool_verify_plugin_state

    def _patched(*, plugin_name, **kw):
        return original(
            plugin_name=plugin_name,
            _tools_bin=plugin_layout["tools_bin"],
            _cache_root=plugin_layout["cache_root"],
        )

    with patch.object(tools_mod, "_tool_verify_plugin_state", side_effect=_patched):
        result = _tool_verify_plugin_secrets(plugin_name="face-rec")

    assert "secrets" in result
    assert isinstance(result["secrets"], list)
