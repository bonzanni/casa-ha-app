"""POSIX-env reader/writer for /addon_configs/casa-agent/plugin-env.conf."""
from __future__ import annotations

import os
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode bits not meaningful on Windows")
def test_first_creation_is_0600_even_without_chmod(tmp_path: Path, monkeypatch) -> None:
    """The secrets file must be *born* 0600 — not created world-readable and
    repaired afterwards. Simulate chmod being unavailable (crash/permission
    denial between write and chmod) and assert the mode is still 0600."""
    path = tmp_path / "plugin-env.conf"
    monkeypatch.setattr("plugin_env_conf.PLUGIN_ENV_CONF_PATH", path)

    def _chmod_denied(*args, **kwargs):
        raise PermissionError("simulated: no chmod between write and repair")

    monkeypatch.setattr("plugin_env_conf.os.chmod", _chmod_denied)

    old_umask = os.umask(0o022)  # production s6 container umask
    try:
        set_entry("OPENWEATHER_API_KEY", "literal-secret-key")
    finally:
        os.umask(old_umask)

    assert path.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# v0.111.0 (#236) — remove_entry
# ---------------------------------------------------------------------------

def test_remove_entry_deletes_line(tmp_path: Path, monkeypatch) -> None:
    from plugin_env_conf import read_entries, remove_entry, set_entry
    path = tmp_path / "plugin-env.conf"
    monkeypatch.setattr("plugin_env_conf.PLUGIN_ENV_CONF_PATH", path)
    set_entry("FOO_KEY", "v1")
    set_entry("BAR_KEY", "v2")
    assert remove_entry("FOO_KEY") is True
    entries = read_entries()
    assert "FOO_KEY" not in entries
    assert entries["BAR_KEY"] == "v2"


def test_remove_entry_absent_is_false(tmp_path: Path, monkeypatch) -> None:
    from plugin_env_conf import remove_entry, set_entry
    path = tmp_path / "plugin-env.conf"
    monkeypatch.setattr("plugin_env_conf.PLUGIN_ENV_CONF_PATH", path)
    set_entry("FOO_KEY", "v1")
    assert remove_entry("NOPE_KEY") is False
    # no file at all → False, no file created
    monkeypatch.setattr("plugin_env_conf.PLUGIN_ENV_CONF_PATH", tmp_path / "absent.conf")
    assert remove_entry("FOO_KEY") is False
    assert not (tmp_path / "absent.conf").exists()


def test_remove_entry_preserves_comments_and_mode(tmp_path: Path, monkeypatch) -> None:
    import os as _os
    from plugin_env_conf import remove_entry, set_entry
    path = tmp_path / "plugin-env.conf"
    monkeypatch.setattr("plugin_env_conf.PLUGIN_ENV_CONF_PATH", path)
    set_entry("FOO_KEY", "v1")
    header = path.read_text().splitlines()[0]
    assert header.startswith("#")
    assert remove_entry("FOO_KEY") is True
    text = path.read_text()
    assert text.splitlines()[0] == header          # comment preserved
    assert "FOO_KEY" not in text
    assert _os.stat(path).st_mode & 0o777 == 0o600


def test_remove_entry_rejects_bad_name(tmp_path: Path, monkeypatch) -> None:
    import pytest
    from plugin_env_conf import PluginEnvConfError, remove_entry
    monkeypatch.setattr("plugin_env_conf.PLUGIN_ENV_CONF_PATH", tmp_path / "p.conf")
    with pytest.raises(PluginEnvConfError):
        remove_entry("bad-name")
