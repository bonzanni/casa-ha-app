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

# NOTE: test_workspace_template_shape removed in v0.71.0 — render_workspace_
# template no longer writes enabledPlugins (executor plugins load via
# --plugin-dir); see test_workspace_template_renders.py. The agent-home
# enabledPlugins seeding above is retired in the Task 19 deletion sweep.
