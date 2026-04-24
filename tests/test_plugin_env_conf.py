"""POSIX-env reader/writer for /addon_configs/casa-agent/plugin-env.conf."""
from __future__ import annotations

from pathlib import Path

import pytest

from plugin_env_conf import read_entries, set_entry, PluginEnvConfError

pytestmark = pytest.mark.unit


def test_roundtrip(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "plugin-env.conf"
    monkeypatch.setattr("plugin_env_conf.PLUGIN_ENV_CONF_PATH", path)

    set_entry("AWS_REGION", "eu-central-1")
    set_entry("AWS_ACCESS_KEY_ID", "op://Casa/AWS/access_key")
    entries = read_entries()
    assert entries == {
        "AWS_REGION": "eu-central-1",
        "AWS_ACCESS_KEY_ID": "op://Casa/AWS/access_key",
    }


def test_update_existing(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "plugin-env.conf"
    monkeypatch.setattr("plugin_env_conf.PLUGIN_ENV_CONF_PATH", path)
    set_entry("X", "v1")
    set_entry("X", "v2")
    assert read_entries()["X"] == "v2"


def test_rejects_bad_var_name(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "plugin-env.conf"
    monkeypatch.setattr("plugin_env_conf.PLUGIN_ENV_CONF_PATH", path)
    with pytest.raises(PluginEnvConfError):
        set_entry("not-a-var", "x")


def test_preserves_comments(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "plugin-env.conf"
    path.write_text(
        "# Managed by Configurator. Edit via Configurator to avoid sync loss.\n"
        "A=1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("plugin_env_conf.PLUGIN_ENV_CONF_PATH", path)
    set_entry("B", "2")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Managed by Configurator.")
    assert "A=1" in text
    assert "B=2" in text
