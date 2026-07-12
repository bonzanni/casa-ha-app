"""Workspace-template rendering (§16.3, unified plugin arch §3.3): copy the
template subtree + generate .claude/settings.json with hooks + permissions
(NO enabledPlugins — executor plugins load via --plugin-dir), plus the
run-script --plugin-dir plumbing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def executor_defaults(tmp_path: Path) -> Path:
    """Minimal defaults tree with plugin-developer + test-fixture executors."""
    base = tmp_path / "defaults" / "agents" / "executors"
    for name in ("plugin-developer", "test-fixture"):
        root = base / name
        (root / "workspace-template" / ".claude").mkdir(parents=True)
        (root / "workspace-template" / "CLAUDE.md.tmpl").write_text(
            "# {executor_type} engagement\n\nTask: {task}\n\nContext: {context}\n\n"
            "World state:\n{world_state_summary}\n",
            encoding="utf-8",
        )
    return base


def _defn(exec_type="test-fixture", tools_allowed=("Read",),
          permission_mode="acceptEdits"):
    from config import ExecutorDefinition
    return ExecutorDefinition(
        type=exec_type,
        description="test fixture twenty-character description here",
        model="sonnet",
        driver="claude_code",
        tools_allowed=list(tools_allowed),
        permission_mode=permission_mode,
    )


def _render(executor_defaults, exec_type, defn, dest, **kw):
    from drivers.workspace import render_workspace_template
    render_workspace_template(
        template_root=executor_defaults / exec_type / "workspace-template",
        dest=dest, defn=defn, executor_type=exec_type,
        task=kw.get("task", "t"), context=kw.get("context", "c"),
        world_state_summary=kw.get("world_state_summary", ""),
        hooks_yaml_data=kw.get("hooks_yaml_data", {}),
    )


def test_renders_claude_md(tmp_path, executor_defaults):
    dest = tmp_path / "engagement"
    _render(executor_defaults, "plugin-developer",
            _defn("plugin-developer"), dest,
            task="build face-rec", context="targets=tina,ellen",
            world_state_summary="(none)")
    claude_md = (dest / "CLAUDE.md").read_text(encoding="utf-8")
    assert "# plugin-developer engagement" in claude_md
    assert "Task: build face-rec" in claude_md
    assert "targets=tina,ellen" in claude_md


def test_generates_settings_json_without_enabled_plugins(tmp_path, executor_defaults):
    """§3.3: settings.json has hooks + permissions but NO enabledPlugins."""
    dest = tmp_path / "engagements" / "eng1"
    _render(executor_defaults, "test-fixture",
            _defn(tools_allowed=["Read", "Bash(git*)"]), dest)
    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert "enabledPlugins" not in settings
    assert "hooks" in settings
    assert settings["permissions"]["allow"] == ["Read", "Bash(git*)"]
    assert settings["permissions"]["defaultMode"] == "acceptEdits"


def test_template_path_filters_invalid_permissions(tmp_path, executor_defaults):
    dest = tmp_path / "engagements" / "eng2"
    _render(executor_defaults, "test-fixture",
            _defn(tools_allowed=["Read", "casa-internal-bogus", "Bash(git*)"],
                  permission_mode="bypassPermissions"), dest)
    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["allow"] == ["Read", "Bash(git*)"]
    assert settings["permissions"]["defaultMode"] == "bypassPermissions"


def test_template_keeps_broad_bash_and_web_tools(tmp_path, executor_defaults):
    dest = tmp_path / "engagements" / "eng3"
    _render(executor_defaults, "test-fixture",
            _defn(tools_allowed=["Bash", "WebFetch", "WebSearch", "Read",
                                 "casa-internal-bogus"],
                  permission_mode="auto"), dest)
    allow = json.loads((dest / ".claude" / "settings.json").read_text()
                       )["permissions"]["allow"]
    assert "Bash" in allow and "WebFetch" in allow and "WebSearch" in allow
    assert "Read" in allow
    assert "casa-internal-bogus" not in allow


def test_template_path_writes_translated_hooks(tmp_path, executor_defaults):
    dest = tmp_path / "engagements" / "eng4"
    _render(executor_defaults, "test-fixture", _defn(), dest,
            hooks_yaml_data={"pre_tool_use": [{"policy": "block_dangerous_bash"}]})
    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert "PreToolUse" in settings["hooks"]
    assert len(settings["hooks"]["PreToolUse"]) == 1


def test_template_path_handles_bundled_plugin_developer(tmp_path):
    """Regression: bundled plugin-developer definition.yaml + hooks.yaml +
    workspace-template/ flow through render_workspace_template cleanly, and
    settings.json carries NO enabledPlugins (§3.3)."""
    import yaml
    from config import ExecutorDefinition
    from drivers.workspace import render_workspace_template

    here = Path(__file__).resolve().parent.parent
    plugin_dev_dir = (
        here / "casa-agent" / "rootfs" / "opt" / "casa" / "defaults"
        / "agents" / "executors" / "plugin-developer"
    )
    raw_defn = yaml.safe_load((plugin_dev_dir / "definition.yaml").read_text(encoding="utf-8"))
    raw_hooks = yaml.safe_load((plugin_dev_dir / "hooks.yaml").read_text(encoding="utf-8")) or {}
    tools = raw_defn.get("tools") or {}
    defn = ExecutorDefinition(
        type=raw_defn["type"], description=raw_defn["description"],
        model="sonnet", driver=raw_defn["driver"],
        tools_allowed=list(tools.get("allowed", [])),
        permission_mode=tools.get("permission_mode", "acceptEdits"),
    )
    dest = tmp_path / "engagements" / "eng-bundled"
    render_workspace_template(
        template_root=plugin_dev_dir / "workspace-template",
        dest=dest, defn=defn, hooks_yaml_data=raw_hooks,
        executor_type=defn.type, task="probe task", context="probe context",
        world_state_summary="",
    )
    settings = json.loads((dest / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["allow"] == defn.tools_allowed
    assert settings["permissions"]["defaultMode"] == defn.permission_mode
    assert "PreToolUse" in settings["hooks"]
    assert len(settings["hooks"]["PreToolUse"]) >= 1
    assert "enabledPlugins" not in settings          # §3.3


# --- run-script --plugin-dir plumbing (§3.8) --------------------------------

def test_render_run_script_plugin_dir_flags():
    from drivers.workspace import render_run_script
    out = render_run_script(
        engagement_id="e" * 32, permission_mode="acceptEdits", extra_dirs=[],
        plugin_dirs=["/config/plugins/store/a/aaa",
                     "/config/plugins/store/b/bbb"])
    assert ("--plugin-dir /config/plugins/store/a/aaa "
            "--plugin-dir /config/plugins/store/b/bbb") in out


def test_render_run_script_rejects_relative_or_shell_special_plugin_dir():
    from drivers.workspace import render_run_script, WorkspaceConfigError
    for bad in ("relative/path", "/a;rm -rf /", "/a$(evil)", "/a|b"):
        with pytest.raises(WorkspaceConfigError):
            render_run_script(engagement_id="e" * 32,
                              permission_mode="acceptEdits", extra_dirs=[],
                              plugin_dirs=[bad])


def test_run_template_has_no_seed_or_cache_env():
    template = (Path(__file__).resolve().parent.parent / "casa-agent" / "rootfs"
                / "opt" / "casa" / "scripts" / "engagement_run_template.sh"
                ).read_text(encoding="utf-8")
    assert "CLAUDE_CODE_PLUGIN_SEED_DIR" not in template
    assert "CLAUDE_CODE_PLUGIN_CACHE_DIR" not in template
    assert "{PLUGIN_DIR_FLAGS}" in template


def test_template_path_fires_without_plugins_yaml(tmp_path, executor_defaults):
    """§3.3: the template render path is selected by the template dir alone —
    no plugins.yaml needed."""
    import asyncio
    from drivers.workspace import provision_workspace

    exec_dir = executor_defaults / "test-fixture"
    defn = _defn()
    defn.hooks_path = ""
    defn.prompt_template_path = str(exec_dir / "prompt.md")
    defn.extra_dirs = []
    defn.mcp_server_names = []
    ws = asyncio.run(provision_workspace(
        engagements_root=str(tmp_path / "engagements"),
        engagement_id="f" * 32, defn=defn, task="t", context="c",
        casa_framework_mcp_url="http://x",
        workspace_template_root=exec_dir / "workspace-template",
    ))
    # Template path fired → CLAUDE.md rendered from the .tmpl.
    assert (Path(ws) / "CLAUDE.md").read_text(encoding="utf-8").startswith(
        "# test-fixture engagement")
    settings = json.loads((Path(ws) / ".claude" / "settings.json").read_text())
    assert "enabledPlugins" not in settings
