"""Parse a plugin's .mcp.json and return the set of ${VAR} references
not in CC's built-in allowlist. Plan 4b §4.2 + §7.3 step 6.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CC_BUILTIN_VARS: frozenset[str] = frozenset({
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
    "HOME",
    "PATH",
    "USER",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "PWD",
})

_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def extract_env_vars(mcp_json_path: Path | str) -> set[str]:
    path = Path(mcp_json_path)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc

    servers = data.get("mcpServers") or {}
    vars_found: set[str] = set()
    for server in servers.values():
        env = (server or {}).get("env") or {}
        for val in env.values():
            if not isinstance(val, str):
                continue
            for match in _VAR_PATTERN.finditer(val):
                vars_found.add(match.group(1))
    return vars_found - CC_BUILTIN_VARS
