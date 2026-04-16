"""Safety hooks: command blocking and path-scope enforcement."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

# ---------------------------------------------------------------------------
# Forbidden shell patterns
# ---------------------------------------------------------------------------

FORBIDDEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r"\bcurl\b.*-X\s*POST\b"),
    re.compile(r"\bcurl\b.*--data\b"),
    re.compile(r"\bssh\b"),
    re.compile(r"\bscp\b"),
]

# ---------------------------------------------------------------------------
# Per-agent path rules
# ---------------------------------------------------------------------------

# Each entry maps an agent name to a list of (permission_set, path_prefix) tuples.
# permission_set is a frozenset of tool names (Read, Write, Edit).
AGENT_PATH_RULES: dict[str, list[tuple[frozenset[str], str]]] = {
    "tina": [
        (frozenset({"Read"}), "agents/tina/"),
        (frozenset({"Read"}), "workspace/"),
    ],
    "ellen": [
        (frozenset({"Read"}), "addon_configs/"),
        (frozenset({"Read"}), "/config/"),
        (frozenset({"Write"}), "workspace/"),
    ],
    "plugin-builder": [
        (frozenset({"Read", "Write"}), "workspace/"),
    ],
}


def _deny(reason: str) -> dict[str, Any]:
    """Return a hook-specific output dict that denies the tool call."""
    return {"hookSpecificOutput": {"decision": "deny", "reason": reason}}


def _normalize_path(raw: str) -> str:
    """Resolve ``..`` segments using PurePosixPath splitting (no OS calls).

    Returns the normalized path string without a leading ``/`` unless the
    original path was absolute.
    """
    parts = PurePosixPath(raw).parts
    resolved: list[str] = []
    for part in parts:
        if part == "..":
            if resolved and resolved[-1] != "/":
                resolved.pop()
        elif part != ".":
            resolved.append(part)
    return "/".join(resolved)


# ---------------------------------------------------------------------------
# Hook implementations
# ---------------------------------------------------------------------------


async def block_dangerous_commands(
    input_data: dict[str, Any],
    tool_use_id: str,
    context: dict[str, Any],
) -> dict[str, Any] | None:
    """Block shell commands that match FORBIDDEN_PATTERNS.

    Returns a deny dict if the command is dangerous, else ``None``.
    Only inspects tools named ``Bash``.
    """
    tool_name = input_data.get("tool_name", "")
    if tool_name != "Bash":
        return None

    command = input_data.get("tool_input", {}).get("command", "")
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(command):
            return _deny(f"Blocked by safety hook: {pattern.pattern}")
    return None


async def enforce_path_scope(
    input_data: dict[str, Any],
    tool_use_id: str,
    context: dict[str, Any],
) -> dict[str, Any] | None:
    """Enforce per-agent file-path restrictions.

    Checks Read / Write / Edit tool calls against AGENT_PATH_RULES.
    Returns a deny dict if the path is out of scope, else ``None``.
    """
    tool_name = input_data.get("tool_name", "")
    if tool_name not in ("Read", "Write", "Edit"):
        return None

    agent_name = context.get("agent_name", "")
    rules = AGENT_PATH_RULES.get(agent_name)
    if rules is None:
        # No rules defined for this agent -- allow by default
        return None

    raw_path = input_data.get("tool_input", {}).get("file_path", "")
    norm = _normalize_path(raw_path)

    for perms, prefix in rules:
        if tool_name in perms and norm.startswith(prefix):
            return None  # allowed

    return _deny(
        f"Agent '{agent_name}' is not allowed to {tool_name} path '{raw_path}'"
    )
