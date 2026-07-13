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

from claude_agent_sdk import PermissionResultDeny

logger = logging.getLogger(__name__)

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_segment(s: str) -> str:
    """Documented CC sanitization for namespace segments."""
    return _SANITIZE_RE.sub("_", s)


def _mcp_servers(mcp_json_path: Path) -> dict:
    """The {server-name: config} map from a plugin ``.mcp.json`` — delegates to
    the shared, stdlib-only ``plugin_store.mcp_servers_map`` (also used by the
    build-time verifier) so both understand the wrapper AND top-level shapes."""
    from plugin_store import mcp_servers_map
    return mcp_servers_map(mcp_json_path)


def mcp_json_malformed(rp) -> bool:
    """Sol #16: True iff ``.mcp.json`` is PRESENT but unparseable, not a JSON
    object, or has a non-mapping ``mcpServers`` — i.e. the plugin's declared MCP
    server cannot load. An ABSENT ``.mcp.json`` (skill-only plugin) is NOT
    malformed. Verify uses this so a broken MCP config can't report ready when
    grants/secrets silently degrade to ``[]`` (which is indistinguishable from
    skill-only otherwise)."""
    mcp_json = Path(rp.path) / ".mcp.json"
    if not mcp_json.is_file():
        return False
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict):
        return True
    servers = data.get("mcpServers")
    # No mcpServers key = a valid server-less config. A present-but-non-mapping
    # mcpServers is malformed.
    return servers is not None and not isinstance(servers, dict)


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
