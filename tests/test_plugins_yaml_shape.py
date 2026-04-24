"""Assert settings.json written by provision_agent_home + render_workspace_template
has `enabledPlugins` as an object with @-suffixed string keys mapping to booleans
(spike §Gate 2)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from agent_home import provision_agent_home
from drivers.workspace import render_workspace_template

pytestmark = pytest.mark.unit

KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*@(casa-plugins|casa-plugins-defaults)$")


@pytest.fixture
def defaults_tree(tmp_path: Path) -> Path:
    (tmp_path / "defaults" / "agents" / "ellen").mkdir(parents=True)
    (tmp_path / "defaults" / "agents" / "ellen" / "plugins.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "plugins": [{"name": "superpowers", "marketplace": "casa-plugins-defaults"}],
        }),
        encoding="utf-8",
    )
    return tmp_path


def test_agent_home_shape(tmp_path: Path, defaults_tree: Path) -> None:
    home_root = tmp_path / "agent-home"
    provision_agent_home(role="ellen", home_root=home_root, defaults_root=defaults_tree)
    data = json.loads((home_root / "ellen" / ".claude" / "settings.json").read_text())
    assert isinstance(data["enabledPlugins"], dict), "must be object not list"
    for key, value in data["enabledPlugins"].items():
        assert KEY_PATTERN.match(key), f"key {key!r} fails @marketplace shape"
        assert value is True, f"value for {key!r} must be bool True"


def test_workspace_template_shape(tmp_path: Path) -> None:
    tmpl_root = tmp_path / "tmpl"
    (tmpl_root / ".claude").mkdir(parents=True)
    (tmpl_root / "CLAUDE.md.tmpl").write_text("x", encoding="utf-8")
    plugins_yaml = tmp_path / "plugins.yaml"
    plugins_yaml.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "plugins": [{"name": "claude-dev", "marketplace": "casa-plugins-defaults"}],
        }),
        encoding="utf-8",
    )
    dest = tmp_path / "ws"
    render_workspace_template(
        template_root=tmpl_root,
        plugins_yaml=plugins_yaml,
        dest=dest,
        executor_type="plugin-developer",
        task="",
        context="",
        world_state_summary="",
    )
    data = json.loads((dest / ".claude" / "settings.json").read_text())
    assert isinstance(data["enabledPlugins"], dict)
    for key in data["enabledPlugins"]:
        assert KEY_PATTERN.match(key)
