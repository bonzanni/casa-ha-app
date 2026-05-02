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


# O-3 fixtures: project-scope plugins are reported by the CLI as
# `enabled: false` when run from cc-home (because cc-home's settings.json
# doesn't list them — they live in agent-home/<role>/.claude/settings.json).
# The binding must filter project-scope by projectPath, not by `enabled`.
SAMPLE_LIST_JSON_WITH_PROJECT_SCOPE = json.dumps([
    # User-scope-enabled default plugin (resident SDK should always see it).
    {
        "id": "superpowers@casa-plugins-defaults",
        "version": "5.0.7",
        "scope": "user",
        "enabled": True,
        "installPath": "/opt/claude-seed/cache/casa-plugins-defaults/superpowers/5.0.7",
    },
    # User-scope-disabled plugin (binding still filters this out).
    {
        "id": "user-disabled@casa-plugins",
        "version": "0.0.1",
        "scope": "user",
        "enabled": False,
        "installPath": "/addon_configs/casa-agent/cc-home/.claude/plugins/cache/casa-plugins/user-disabled/0.0.1",
    },
    # Project-scope plugin installed into the assistant role (Casa --scope project).
    # CLI reports enabled=false because cc-home's settings.json doesn't list it,
    # but it IS enabled in agent-home/assistant/.claude/settings.json. O-3.
    {
        "id": "casa-probe-greet@casa-plugins",
        "version": "1.0.0",
        "scope": "project",
        "enabled": False,
        "installPath": "/addon_configs/casa-agent/cc-home/.claude/plugins/cache/casa-plugins/casa-probe-greet/1.0.0",
        "projectPath": "/addon_configs/casa-agent/agent-home/assistant",
    },
    # Project-scope plugin installed into a DIFFERENT role (butler).
    # The assistant binding must not pick this up.
    {
        "id": "butler-only-plugin@casa-plugins",
        "version": "0.0.1",
        "scope": "project",
        "enabled": False,
        "installPath": "/addon_configs/casa-agent/cc-home/.claude/plugins/cache/casa-plugins/butler-only-plugin/0.0.1",
        "projectPath": "/addon_configs/casa-agent/agent-home/butler",
    },
])


@patch("plugins_binding.subprocess.run")
def test_build_sdk_plugins_role_includes_matching_project_scope(mock_run, tmp_path: Path) -> None:
    """O-3 regression: when role is provided, project-scope plugins whose
    projectPath matches /addon_configs/casa-agent/agent-home/<role> must
    be included regardless of the CLI's `enabled` field (which is
    evaluated against cc-home's settings.json, not against the project's
    own settings.json, so it always reports False for Casa-installed
    project-scope plugins)."""
    mock_run.return_value.stdout = SAMPLE_LIST_JSON_WITH_PROJECT_SCOPE
    mock_run.return_value.returncode = 0

    plugins = build_sdk_plugins(
        home=tmp_path / "home",
        shared_cache=tmp_path / "cache",
        seed=tmp_path / "seed",
        role="assistant",
    )

    # Expect: the user-scope-enabled default + the assistant-project-scope plugin.
    # NOT: the user-scope-disabled, NOT the butler-project-scope.
    assert plugins == [
        {"type": "local", "path": "/opt/claude-seed/cache/casa-plugins-defaults/superpowers/5.0.7"},
        {"type": "local", "path": "/addon_configs/casa-agent/cc-home/.claude/plugins/cache/casa-plugins/casa-probe-greet/1.0.0"},
    ]


@patch("plugins_binding.subprocess.run")
def test_build_sdk_plugins_role_omits_other_role_project_scope(mock_run, tmp_path: Path) -> None:
    """O-3 regression: project-scope entries whose projectPath does NOT
    match the role's agent-home are filtered out, even when the CLI
    reports them. Cross-role contamination would be a security/privacy
    bug — assistant must not load butler's plugins."""
    mock_run.return_value.stdout = SAMPLE_LIST_JSON_WITH_PROJECT_SCOPE
    mock_run.return_value.returncode = 0

    plugins = build_sdk_plugins(
        home=tmp_path / "home",
        shared_cache=tmp_path / "cache",
        seed=tmp_path / "seed",
        role="butler",
    )

    assert plugins == [
        {"type": "local", "path": "/opt/claude-seed/cache/casa-plugins-defaults/superpowers/5.0.7"},
        {"type": "local", "path": "/addon_configs/casa-agent/cc-home/.claude/plugins/cache/casa-plugins/butler-only-plugin/0.0.1"},
    ]


@patch("plugins_binding.subprocess.run")
def test_build_sdk_plugins_no_role_omits_all_project_scope(mock_run, tmp_path: Path) -> None:
    """O-3 regression: when role is None (specialists + executors call-sites
    which per install.md doctrine don't carry plugins), project-scope
    entries are filtered out entirely. Behavior matches v0.34.2 — only
    user-scope-enabled plugins surface."""
    mock_run.return_value.stdout = SAMPLE_LIST_JSON_WITH_PROJECT_SCOPE
    mock_run.return_value.returncode = 0

    plugins = build_sdk_plugins(
        home=tmp_path / "home",
        shared_cache=tmp_path / "cache",
        seed=tmp_path / "seed",
    )

    # Only the user-scope-enabled default. No project-scope leakage.
    assert plugins == [
        {"type": "local", "path": "/opt/claude-seed/cache/casa-plugins-defaults/superpowers/5.0.7"},
    ]
