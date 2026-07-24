"""Read/write /config/plugin-env.conf (§5.5).

File is mode 0600. Preserves comments. set_entry upserts a single VAR=value
line; remove_entry deletes one (v0.111.0, #236).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PLUGIN_ENV_CONF_PATH: Path = Path("/config/plugin-env.conf")

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
    # Create with 0600 atomically so secret content never exists on disk
    # with group/other read bits (contract: "File is mode 0600").
    fd = os.open(PLUGIN_ENV_CONF_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join(lines))
    try:
        os.chmod(PLUGIN_ENV_CONF_PATH, 0o600)  # belt-and-braces: repairs a legacy 0644 file
    except PermissionError:
        pass  # non-root in tests


def remove_entry(var_name: str) -> bool:
    """Delete the ``VAR=...`` line for *var_name* (v0.111.0, #236).

    Returns ``True`` when a line was removed, ``False`` when the var was not
    present (idempotent — callers report, they don't error). Comments and
    every other line are preserved byte-for-byte; the rewrite keeps the 0600
    contract. The caller MUST follow with ``casa_reload(scope='plugin_env')``
    — the reload's deletion-diff (M22) pops the removed key from the
    effective environment and regenerates plugin health.
    """
    if not _VAR_NAME_RE.match(var_name):
        raise PluginEnvConfError(f"invalid env var name: {var_name!r}")
    if not PLUGIN_ENV_CONF_PATH.is_file():
        return False

    lines = PLUGIN_ENV_CONF_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    kept: list[str] = []
    removed = False
    for line in lines:
        if (not line.lstrip().startswith("#") and "=" in line
                and line.split("=", 1)[0].strip() == var_name):
            removed = True
            continue
        kept.append(line)
    if not removed:
        return False

    fd = os.open(PLUGIN_ENV_CONF_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join(kept))
    try:
        os.chmod(PLUGIN_ENV_CONF_PATH, 0o600)
    except PermissionError:
        pass  # non-root in tests
    return True
