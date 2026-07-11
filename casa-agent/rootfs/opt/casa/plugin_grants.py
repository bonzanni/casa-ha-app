"""P-5: plugin MCP-tool grants derived from installed state (spec
2026-07-11-p5-plugin-tool-grants-design.md, amended 2026-07-12).

Namespace (documented — code.claude.com/docs/en/mcp.md "Plugin MCP tool
names"): ``mcp__plugin_<plugin>_<server>__<tool>``, segments sanitized so any
char outside ``A-Za-z0-9_-`` becomes ``_``. A SERVER-LEVEL grant is that
string without the ``__<tool>`` suffix — covers every tool the server exposes,
including ones added by future plugin versions (same prefix rule as
``mcp__homeassistant``; proven live for the plugin form on CC 2.1.150).

Grants derive from the agent-home's ``.claude/settings.json`` →
``enabledPlugins`` (what CC actually loads for that cwd via
``setting_sources=["project"]``), NOT from the binding layer (which filters
project-scope entries for specialists). Derivation never raises into a turn:
missing/corrupt files degrade to no grants at DEBUG.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultDeny

logger = logging.getLogger(__name__)

_CACHE_ROOT = Path("/config/cc-home/.claude/plugins/cache")
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_segment(s: str) -> str:
    """Documented CC sanitization for namespace segments."""
    return _SANITIZE_RE.sub("_", s)


def _version_key(p: Path) -> tuple:
    """Order version dirs: numeric-aware so 1.10.0 > 1.9.0; unparseable
    parts compare as strings after all numeric ones."""
    parts: list[tuple[int, int | str]] = []
    for part in p.name.split("."):
        parts.append((0, int(part)) if part.isdigit() else (1, part))
    return tuple(parts)


def grants_for_plugin(
    name: str, marketplace: str, *, cache_root: Path = _CACHE_ROOT,
) -> list[str]:
    """Server-level grant strings for one cached plugin (sorted).

    Skill-only plugins (no ``.mcp.json``) yield ``[]``. Multiple cached
    versions: the highest version dir wins (matches what a fresh CC load
    resolves after an update)."""
    plugin_dir = Path(cache_root) / marketplace / name
    try:
        versions = [d for d in plugin_dir.iterdir() if d.is_dir()]
    except OSError:
        return []
    if not versions:
        return []
    mcp_json = max(versions, key=_version_key) / ".mcp.json"
    if not mcp_json.is_file():
        return []
    try:
        data: dict[str, Any] = json.loads(mcp_json.read_text(encoding="utf-8"))
        servers = data.get("mcpServers") or {}
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        logger.debug("plugin_grants: unreadable %s: %s", mcp_json, exc)
        return []
    if not isinstance(servers, dict):
        return []
    plugin_seg = sanitize_segment(name)
    return sorted(
        f"mcp__plugin_{plugin_seg}_{sanitize_segment(server)}"
        for server in servers
    )


def derived_plugin_grants(
    agent_home: str | Path, *, cache_root: Path = _CACHE_ROOT,
) -> list[str]:
    """Union of server-level grants for every plugin enabled in
    ``<agent_home>/.claude/settings.json`` (sorted, deduplicated)."""
    settings = Path(agent_home) / ".claude" / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        enabled = data.get("enabledPlugins") or {}
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        logger.debug("plugin_grants: no usable settings at %s: %s", settings, exc)
        return []
    if not isinstance(enabled, dict):
        return []
    grants: set[str] = set()
    for key, is_enabled in enabled.items():
        if not is_enabled or "@" not in str(key):
            continue
        name, _, marketplace = str(key).partition("@")
        grants.update(grants_for_plugin(name, marketplace, cache_root=cache_root))
    return sorted(grants)


def make_fail_closed_can_use_tool(role: str):
    """Fail-closed ``can_use_tool`` for in-casa agents (P-5b).

    The SDK consults this ONLY for tool calls not already auto-approved via
    ``allowed_tools``/``permission_mode`` — granted tools never reach it. With
    no callback, an ungranted call falls through to CC's interactive prompt,
    which nothing on the in-casa path can answer (no relay) → headless hang.
    Deny fast and loud instead. No awaits inside, so caller cancellation
    (voice barge-in) has nothing to be swallowed by.
    """
    async def _deny(tool_name: str, tool_input: dict, context) -> PermissionResultDeny:
        logger.warning(
            "fail-closed deny: tool=%s role=%s (not in allowed_tools)",
            tool_name, role,
        )
        return PermissionResultDeny(
            message=(
                f"{tool_name} is not granted to {role!r}; grant it via the "
                "configurator or install the plugin that provides it."
            ),
            interrupt=False,
        )
    return _deny
