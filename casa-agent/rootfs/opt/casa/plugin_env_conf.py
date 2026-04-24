"""Read/write /addon_configs/casa-agent/plugin-env.conf (§5.5).

File is mode 0600. Preserves comments. set_entry upserts a single VAR=value line.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PLUGIN_ENV_CONF_PATH: Path = Path("/addon_configs/casa-agent/plugin-env.conf")

_HEADER = (
    "# Managed by Configurator. Edit via Configurator to avoid sync loss.\n"
)
_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class PluginEnvConfError(ValueError):
    """Raised on invalid var names or I/O failure."""


def read_entries() -> dict[str, str]:
    if not PLUGIN_ENV_CONF_PATH.is_file():
        return {}
    entries: dict[str, str] = {}
    for line in PLUGIN_ENV_CONF_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        entries[name.strip()] = value.strip()
    return entries


def set_entry(var_name: str, value: str) -> None:
    if not _VAR_NAME_RE.match(var_name):
        raise PluginEnvConfError(f"invalid env var name: {var_name!r}")

    lines: list[str] = []
    if PLUGIN_ENV_CONF_PATH.is_file():
        lines = PLUGIN_ENV_CONF_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        lines = [_HEADER]

    replaced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            continue
        if line.split("=", 1)[0].strip() == var_name:
            lines[i] = f"{var_name}={value}\n"
            replaced = True
            break

    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(f"{var_name}={value}\n")

    PLUGIN_ENV_CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLUGIN_ENV_CONF_PATH.write_text("".join(lines), encoding="utf-8")
    try:
        os.chmod(PLUGIN_ENV_CONF_PATH, 0o600)
    except PermissionError:
        pass  # non-root in tests
