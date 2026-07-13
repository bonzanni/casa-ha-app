"""Parse a plugin's .mcp.json and return the set of ${VAR} references
not in CC's built-in allowlist. Plan 4b §4.2 + §7.3 step 6.
"""
from __future__ import annotations

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
    # Sol CI-review: resolve servers via the ONE shared parser so secrets are
    # extracted for BOTH the mcpServers wrapper AND the top-level shape (context7
    # ships the latter) — otherwise a top-level plugin's required secrets would be
    # silently missed and verification could report ready without them.
    from plugin_store import mcp_servers_map
    vars_found: set[str] = set()
    for server in mcp_servers_map(mcp_json_path).values():
        env = (server or {}).get("env") or {}
        for val in env.values():
            if not isinstance(val, str):
                continue
            for match in _VAR_PATTERN.finditer(val):
                vars_found.add(match.group(1))
    return vars_found - CC_BUILTIN_VARS
