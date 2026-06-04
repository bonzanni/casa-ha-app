"""Config dir is the Supervisor-managed /config mount, not the old base-slug path (v0.46.0)."""
import pathlib
import re

import pytest

import casa_core

pytestmark = [pytest.mark.unit]

_OPT_CASA = pathlib.Path(casa_core.__file__).parent


def test_config_dir_is_slash_config():
    assert casa_core.CONFIG_DIR == "/config"


def test_no_addon_configs_casa_agent_literal_in_python():
    offenders = []
    for p in _OPT_CASA.rglob("*.py"):
        if re.search(r"/addon_configs/casa-agent", p.read_text(encoding="utf-8")):
            offenders.append(str(p.relative_to(_OPT_CASA)))
    assert offenders == [], f"stale /addon_configs/casa-agent literal in: {offenders}"
