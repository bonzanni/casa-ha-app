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


def test_provision_all_homes_includes_residents_and_specialists(tmp_path: Path) -> None:
    """E-4 regression: residents AND specialists must each get an
    agent-home dir at boot. Pre-Phase-2 the inline loop in
    casa_core.main iterated `role_configs` only and silently skipped
    every specialist."""
    from agent_home import provision_all_homes

    home_root = tmp_path / "agent-home"
    defaults_root = tmp_path
    _write_plugins_yaml(defaults_root, "assistant", [])
    _write_plugins_yaml(defaults_root, "butler", [])
    _write_plugins_yaml(defaults_root, "finance", [])

    role_configs = {"assistant": object(), "butler": object()}
    specialist_configs = {"finance": object()}

    provision_all_homes(
        role_configs=role_configs,
        specialist_configs=specialist_configs,
        home_root=home_root,
        defaults_root=defaults_root,
    )

    for role in ("assistant", "butler", "finance"):
        settings = home_root / role / ".claude" / "settings.json"
        assert settings.is_file(), f"missing {role} settings.json"
        body = json.loads(settings.read_text(encoding="utf-8"))
        assert body == {"enabledPlugins": {}}


def test_provision_all_homes_excludes_executors(tmp_path: Path) -> None:
    """Executors are intentionally NOT provisioned an agent-home —
    they run with cwd=/addon_configs/casa-agent (see
    tools.py::_build_executor_options:257). This test verifies the
    helper is purely role-driven: it only creates dirs for roles
    present in its inputs. Pass only `assistant`; assert no
    `configurator/` or `finance/` dir spontaneously appears. (Does
    NOT guard against a caller mistakenly placing an executor key in
    role_configs/specialist_configs — that's a caller-side contract.)
    """
    from agent_home import provision_all_homes

    home_root = tmp_path / "agent-home"
    defaults_root = tmp_path
    _write_plugins_yaml(defaults_root, "assistant", [])

    provision_all_homes(
        role_configs={"assistant": object()},
        specialist_configs={},
        home_root=home_root,
        defaults_root=defaults_root,
    )

    assert (home_root / "assistant").is_dir()
    assert not (home_root / "configurator").exists()
    assert not (home_root / "finance").exists()


def test_provision_all_homes_continues_on_individual_failure(
    tmp_path: Path, caplog,
) -> None:
    """If one role's plugins.yaml is malformed, that role fails its
    provisioning step — but every OTHER role must still get its
    agent-home. Exercises the per-role try/except inside
    `agent_home.provision_all_homes` (the BLE001-noqa block)."""
    import logging
    from agent_home import provision_all_homes

    home_root = tmp_path / "agent-home"
    defaults_root = tmp_path
    _write_plugins_yaml(defaults_root, "assistant", [])
    # butler's plugins.yaml has an unsupported schema_version which
    # forces load_plugins_yaml to raise PluginsConfigError, exercising
    # the per-role try/except guard inside provision_all_homes.
    bad = defaults_root / "defaults" / "agents" / "butler" / "plugins.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(yaml.safe_dump({"schema_version": 999, "plugins": []}),
                   encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_home"):
        provision_all_homes(
            role_configs={"assistant": object(), "butler": object()},
            specialist_configs={},
            home_root=home_root,
            defaults_root=defaults_root,
        )

    # assistant got provisioned; butler did not.
    assert (home_root / "assistant" / ".claude" / "settings.json").is_file()
    assert not (home_root / "butler" / ".claude" / "settings.json").exists()
    # warning emitted for the failing role.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("butler" in r.message for r in warnings), (
        f"expected a warning naming butler, got: "
        f"{[r.message for r in warnings]}"
    )
