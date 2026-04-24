"""Workspace-template rendering (§16.3): copy template subtree + generate
.claude/settings.json with enabledPlugins from plugins.yaml."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


@pytest.fixture
def executor_defaults(tmp_path: Path) -> Path:
    root = tmp_path / "defaults" / "agents" / "executors" / "plugin-developer"
    (root / "workspace-template" / ".claude").mkdir(parents=True)
    (root / "workspace-template" / "CLAUDE.md.tmpl").write_text(
        "# {executor_type} engagement\n\nTask: {task}\n\nContext: {context}\n\n"
        "World state:\n{world_state_summary}\n",
        encoding="utf-8",
    )
    (root / "plugins.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "plugins": [
                {"name": "superpowers", "marketplace": "casa-plugins-defaults"},
                {"name": "claude-dev", "marketplace": "casa-plugins-defaults"},
            ],
        }),
        encoding="utf-8",
    )
    return root


def test_renders_claude_md(tmp_path: Path, executor_defaults: Path) -> None:
    from drivers.workspace import render_workspace_template

    dest = tmp_path / "engagement"
    render_workspace_template(
        template_root=executor_defaults / "workspace-template",
        plugins_yaml=executor_defaults / "plugins.yaml",
        dest=dest,
        executor_type="plugin-developer",
        task="build face-rec",
        context="targets=tina,ellen",
        world_state_summary="(none)",
    )
    claude_md = (dest / "CLAUDE.md").read_text(encoding="utf-8")
    assert "# plugin-developer engagement" in claude_md
    assert "Task: build face-rec" in claude_md
    assert "targets=tina,ellen" in claude_md


def test_generates_settings_json_from_plugins_yaml(tmp_path: Path, executor_defaults: Path) -> None:
    from drivers.workspace import render_workspace_template

    dest = tmp_path / "engagement"
    render_workspace_template(
        template_root=executor_defaults / "workspace-template",
        plugins_yaml=executor_defaults / "plugins.yaml",
        dest=dest,
        executor_type="plugin-developer",
        task="t",
        context="c",
        world_state_summary="",
    )
    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert settings == {"enabledPlugins": {
        "superpowers@casa-plugins-defaults": True,
        "claude-dev@casa-plugins-defaults": True,
    }}
