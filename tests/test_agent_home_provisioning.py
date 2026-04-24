"""Agent-home provisioning + default plugin seeding at casa_core boot."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

pytestmark = pytest.mark.unit


def _write_plugins_yaml(root: Path, role: str, plugins: list[dict]) -> None:
    p = root / "defaults" / "agents" / role / "plugins.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"schema_version": 1, "plugins": plugins}), encoding="utf-8")


def test_provision_creates_empty_settings(tmp_path: Path) -> None:
    from agent_home import provision_agent_home

    home_root = tmp_path / "agent-home"
    defaults_root = tmp_path
    _write_plugins_yaml(defaults_root, "ellen", [])

    provision_agent_home(
        role="ellen",
        home_root=home_root,
        defaults_root=defaults_root,
    )
    settings = json.loads((home_root / "ellen" / ".claude" / "settings.json").read_text())
    assert settings == {"enabledPlugins": {}}


def test_provision_seeds_enabled_plugins(tmp_path: Path) -> None:
    from agent_home import provision_agent_home

    home_root = tmp_path / "agent-home"
    defaults_root = tmp_path
    _write_plugins_yaml(defaults_root, "ellen", [
        {"name": "superpowers", "marketplace": "casa-plugins-defaults"},
        {"name": "face-rec", "marketplace": "casa-plugins"},
    ])

    provision_agent_home(
        role="ellen",
        home_root=home_root,
        defaults_root=defaults_root,
    )
    settings = json.loads((home_root / "ellen" / ".claude" / "settings.json").read_text())
    assert settings["enabledPlugins"] == {
        "superpowers@casa-plugins-defaults": True,
        "face-rec@casa-plugins": True,
    }


def test_provision_preserves_user_enabledplugins(tmp_path: Path) -> None:
    """If the settings.json already exists (user has installed extras),
    default seeding must not clobber user-added entries."""
    from agent_home import provision_agent_home

    home_root = tmp_path / "agent-home"
    defaults_root = tmp_path
    _write_plugins_yaml(defaults_root, "ellen", [
        {"name": "superpowers", "marketplace": "casa-plugins-defaults"},
    ])
    settings_path = home_root / "ellen" / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"enabledPlugins": {
        "face-rec@casa-plugins": True,
        "user-custom@casa-plugins": True,
    }}), encoding="utf-8")

    provision_agent_home(
        role="ellen",
        home_root=home_root,
        defaults_root=defaults_root,
    )
    settings = json.loads(settings_path.read_text())
    assert settings["enabledPlugins"] == {
        "superpowers@casa-plugins-defaults": True,  # added by seed
        "face-rec@casa-plugins": True,               # preserved
        "user-custom@casa-plugins": True,            # preserved
    }
