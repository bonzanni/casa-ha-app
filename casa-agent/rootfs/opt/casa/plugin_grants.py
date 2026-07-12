"""P-5: plugin MCP-tool grants derived from the RESOLVED artifact (unified
plugin architecture, spec §3.4). Grants come from `<ResolvedPlugin.path>/
.mcp.json` — the same resolved object the loader/verify/secrets consume, no
settings scan, no version-dir guessing.

Namespace (documented — code.claude.com/docs/en/mcp.md "Plugin MCP tool
names"): ``mcp__plugin_<plugin>_<server>__<tool>``, segments sanitized so any
char outside ``A-Za-z0-9_-`` becomes ``_``. A SERVER-LEVEL grant is that
string without the ``__<tool>`` suffix — covers every tool the server exposes
(same prefix rule as ``mcp__homeassistant``; proven live for the plugin form
on CC 2.1.150). Derivation never raises into a turn: missing/corrupt files
degrade to no grants at DEBUG.

The resident/specialist/executor OPTION-BUILDER integration tests live in
tests/test_agent_plugin_binding.py (Task 7), not here.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultDeny

logger = logging.getLogger(__name__)

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_segment(s: str) -> str:
    """Documented CC sanitization for namespace segments."""
    return _SANITIZE_RE.sub("_", s)


def _mcp_servers(mcp_json_path: Path) -> dict:
    try:
        data = json.loads(mcp_json_path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers") or {}
        return servers if isinstance(servers, dict) else {}
    except (OSError, ValueError, AttributeError) as exc:
        logger.debug("plugin .mcp.json unreadable (%s): %s",
                     mcp_json_path, exc)
        return {}


def grants_for_resolved(rp) -> list[str]:
    """Server-level grant strings for one resolved plugin (sorted). Skill-only
    plugins (no ``.mcp.json``) yield ``[]``."""
    mcp_json = Path(rp.path) / ".mcp.json"
    if not mcp_json.is_file():
        return []
    plugin_seg = sanitize_segment(rp.name)
    return sorted(
        f"mcp__plugin_{plugin_seg}_{sanitize_segment(server)}"
        for server in _mcp_servers(mcp_json)
    )


def grants_for_resolution(res) -> list[str]:
    """Sorted, deduplicated union of server-level grants for every fully-
    resolved plugin in a ResolutionResult."""
    out: set[str] = set()
    for rp in res.plugins:
        out.update(grants_for_resolved(rp))
    return sorted(out)


def required_env_vars_for_resolved(rp) -> list[str]:
    """Skill-only plugins (no .mcp.json) require nothing; malformed JSON
    degrades to [] at DEBUG — same never-raise contract as grants."""
    mcp_json = Path(rp.path) / ".mcp.json"
    if not mcp_json.is_file():
        return []
    try:
        from plugin_env_extractor import extract_env_vars
        return sorted(extract_env_vars(mcp_json))
    except Exception as exc:  # noqa: BLE001 — never raise into a tool/turn
        logger.debug("env-var extraction failed (%s): %s", mcp_json, exc)
        return []


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


# --- legacy (deleted in Task 7) --------------------------------------------
# Transitional shim: agent.py + tools.py still import these old names until
# their resolver cutover (Task 7) and the verify/marketplace tools until
# Task 14. Kept byte-for-byte so the unit gate stays green at this commit;
# nothing new should call them.

_CACHE_ROOT = Path("/config/cc-home/.claude/plugins/cache")


def _version_key(p: Path) -> tuple:
    parts: list[tuple[int, int | str]] = []
    for part in p.name.split("."):
        parts.append((0, int(part)) if part.isdecimal() else (1, part))
    return tuple(parts)


def highest_version_mcp_json(plugin_dir: str | Path) -> Path | None:
    pd = Path(plugin_dir)
    try:
        versions = [d for d in pd.iterdir() if d.is_dir()]
    except OSError:
        return None
    if not versions:
        return None
    mcp = max(versions, key=_version_key) / ".mcp.json"
    return mcp if mcp.is_file() else None


def grants_for_plugin(
    name: str, marketplace: str, *, cache_root: Path = _CACHE_ROOT,
) -> list[str]:
    plugin_dir = Path(cache_root) / marketplace / name
    try:
        mcp_json = highest_version_mcp_json(plugin_dir)
        if mcp_json is None:
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
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "plugin_grants: derivation failed for %s@%s: %s",
            name, marketplace, exc,
        )
        return []


def derived_plugin_grants(
    agent_home: str | Path, *, cache_root: Path = _CACHE_ROOT,
) -> list[str]:
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
