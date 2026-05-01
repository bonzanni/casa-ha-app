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
    """Create a minimal defaults tree with both plugin-developer and test-fixture executors."""
    base = tmp_path / "defaults" / "agents" / "executors"

    # plugin-developer executor (used by original tests)
    root = base / "plugin-developer"
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

    # test-fixture executor (used by L-1 tests)
    fixture_root = base / "test-fixture"
    (fixture_root / "workspace-template" / ".claude").mkdir(parents=True)
    (fixture_root / "workspace-template" / "CLAUDE.md.tmpl").write_text(
        "# {executor_type} engagement\n\nTask: {task}\n\nContext: {context}\n\n"
        "World state:\n{world_state_summary}\n",
        encoding="utf-8",
    )
    (fixture_root / "plugins.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "plugins": [
                {"name": "superpowers", "marketplace": "casa-plugins-defaults"},
            ],
        }),
        encoding="utf-8",
    )

    return base


def test_renders_claude_md(tmp_path: Path, executor_defaults: Path) -> None:
    from drivers.workspace import render_workspace_template

    dest = tmp_path / "engagement"
    render_workspace_template(
        template_root=executor_defaults / "plugin-developer" / "workspace-template",
        plugins_yaml=executor_defaults / "plugin-developer" / "plugins.yaml",
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
    """settings.json contains enabledPlugins + hooks (empty default) + permissions."""
    from config import ExecutorDefinition
    from drivers.workspace import render_workspace_template

    dest = tmp_path / "engagements" / "eng1"
    defn = ExecutorDefinition(
        type="test-fixture",
        description="test fixture twenty-character description here",
        model="sonnet",
        driver="claude_code",
        tools_allowed=["Read", "Bash(git*)"],
        permission_mode="acceptEdits",
    )
    render_workspace_template(
        template_root=executor_defaults / "test-fixture" / "workspace-template",
        plugins_yaml=executor_defaults / "test-fixture" / "plugins.yaml",
        dest=dest,
        defn=defn,
        hooks_yaml_data={},
        executor_type="test-fixture",
        task="t", context="c",
        world_state_summary="",
    )

    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert "enabledPlugins" in settings
    assert "hooks" in settings
    assert settings["permissions"]["allow"] == ["Read", "Bash(git*)"]
    assert settings["permissions"]["defaultMode"] == "acceptEdits"


def test_template_path_filters_invalid_permissions(tmp_path: Path, executor_defaults: Path) -> None:
    """L-1: template path drops invalid CC permission entries with WARNING."""
    from config import ExecutorDefinition
    from drivers.workspace import render_workspace_template

    dest = tmp_path / "engagements" / "eng2"
    defn = ExecutorDefinition(
        type="test-fixture",
        description="test fixture twenty-character description here",
        model="sonnet",
        driver="claude_code",
        tools_allowed=["Read", "casa-internal-bogus", "Bash(git*)"],
        permission_mode="bypassPermissions",
    )
    render_workspace_template(
        template_root=executor_defaults / "test-fixture" / "workspace-template",
        plugins_yaml=executor_defaults / "test-fixture" / "plugins.yaml",
        dest=dest,
        defn=defn,
        hooks_yaml_data={},
        executor_type="test-fixture",
        task="t", context="c",
        world_state_summary="",
    )
    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["allow"] == ["Read", "Bash(git*)"]
    assert settings["permissions"]["defaultMode"] == "bypassPermissions"


def test_template_path_writes_translated_hooks(tmp_path: Path, executor_defaults: Path) -> None:
    """L-1: template path writes hooks block from hooks_yaml_data."""
    from config import ExecutorDefinition
    from drivers.workspace import render_workspace_template

    dest = tmp_path / "engagements" / "eng3"
    defn = ExecutorDefinition(
        type="test-fixture",
        description="test fixture twenty-character description here",
        model="sonnet",
        driver="claude_code",
        tools_allowed=["Read"],
        permission_mode="acceptEdits",
    )
    hooks_data = {
        "pre_tool_use": [
            {"policy": "block_dangerous_bash"},
        ],
    }
    render_workspace_template(
        template_root=executor_defaults / "test-fixture" / "workspace-template",
        plugins_yaml=executor_defaults / "test-fixture" / "plugins.yaml",
        dest=dest,
        defn=defn,
        hooks_yaml_data=hooks_data,
        executor_type="test-fixture",
        task="t", context="c",
        world_state_summary="",
    )
    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert "PreToolUse" in settings["hooks"]
    assert len(settings["hooks"]["PreToolUse"]) == 1


def test_template_path_handles_bundled_plugin_developer(tmp_path: Path) -> None:
    """L-1 regression: bundled plugin-developer definition.yaml + hooks.yaml +
    plugins.yaml + workspace-template/ all flow through render_workspace_template
    cleanly (zero filtered permissions, hooks present, enabledPlugins from
    plugins.yaml)."""
    import yaml
    from pathlib import Path
    from config import ExecutorDefinition
    from drivers.workspace import render_workspace_template

    here = Path(__file__).resolve().parent.parent
    plugin_dev_dir = (
        here / "casa-agent" / "rootfs" / "opt" / "casa" / "defaults"
        / "agents" / "executors" / "plugin-developer"
    )

    # Load bundled definition.yaml + hooks.yaml.
    raw_defn = yaml.safe_load((plugin_dev_dir / "definition.yaml").read_text(encoding="utf-8"))
    raw_hooks = yaml.safe_load((plugin_dev_dir / "hooks.yaml").read_text(encoding="utf-8")) or {}

    # Build ExecutorDefinition mirroring agent_loader's shape.
    tools = raw_defn.get("tools") or {}
    defn = ExecutorDefinition(
        type=raw_defn["type"],
        description=raw_defn["description"],
        model="sonnet",
        driver=raw_defn["driver"],
        tools_allowed=list(tools.get("allowed", [])),
        permission_mode=tools.get("permission_mode", "acceptEdits"),
    )

    dest = tmp_path / "engagements" / "eng-bundled"
    render_workspace_template(
        template_root=plugin_dev_dir / "workspace-template",
        plugins_yaml=plugin_dev_dir / "plugins.yaml",
        dest=dest,
        defn=defn,
        hooks_yaml_data=raw_hooks,
        executor_type=defn.type,
        task="probe task", context="probe context",
        world_state_summary="",
    )

    settings = json.loads((dest / ".claude" / "settings.json").read_text())

    # All bundled tools_allowed entries are valid CC permission patterns -> no drops.
    assert settings["permissions"]["allow"] == defn.tools_allowed
    assert settings["permissions"]["defaultMode"] == defn.permission_mode

    # Hooks block populated (block_dangerous_bash + path_scope at minimum).
    assert "PreToolUse" in settings["hooks"]
    assert len(settings["hooks"]["PreToolUse"]) >= 1

    # enabledPlugins reflects plugin-developer's plugins.yaml.
    assert "enabledPlugins" in settings
    assert isinstance(settings["enabledPlugins"], dict)
