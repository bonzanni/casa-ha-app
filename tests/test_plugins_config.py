"""Schema validation + loader for plugins.yaml."""
from __future__ import annotations

from pathlib import Path

import pytest

from plugins_config import (
    PluginEntry,
    PluginsConfig,
    PluginsConfigError,
    load_plugins_yaml,
)

pytestmark = pytest.mark.unit


def test_load_valid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "plugins.yaml"
    p.write_text(
        "schema_version: 1\n"
        "plugins:\n"
        "  - name: superpowers\n"
        "    marketplace: casa-plugins-defaults\n"
        "    version: 5.0.7\n"
        "  - name: face-rec\n"
        "    marketplace: casa-plugins\n",
        encoding="utf-8",
    )
    cfg = load_plugins_yaml(p)
    assert isinstance(cfg, PluginsConfig)
    assert len(cfg.plugins) == 2
    assert cfg.plugins[0] == PluginEntry(name="superpowers",
                                         marketplace="casa-plugins-defaults",
                                         version="5.0.7")
    assert cfg.plugins[1].version is None


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    cfg = load_plugins_yaml(tmp_path / "nonexistent.yaml")
    assert cfg.plugins == []


def test_bad_schema_version(tmp_path: Path) -> None:
    p = tmp_path / "plugins.yaml"
    p.write_text("schema_version: 2\nplugins: []\n", encoding="utf-8")
    with pytest.raises(PluginsConfigError):
        load_plugins_yaml(p)


def test_bad_marketplace(tmp_path: Path) -> None:
    p = tmp_path / "plugins.yaml"
    p.write_text(
        "schema_version: 1\n"
        "plugins:\n"
        "  - name: foo\n"
        "    marketplace: not-real\n",
        encoding="utf-8",
    )
    with pytest.raises(PluginsConfigError):
        load_plugins_yaml(p)


def test_iter_refs() -> None:
    cfg = PluginsConfig(plugins=[
        PluginEntry(name="a", marketplace="casa-plugins-defaults", version=None),
        PluginEntry(name="b", marketplace="casa-plugins", version="1.0.0"),
    ])
    assert list(cfg.iter_refs()) == ["a@casa-plugins-defaults", "b@casa-plugins"]
