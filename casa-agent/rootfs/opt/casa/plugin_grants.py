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
from pathlib import Path

from claude_agent_sdk import PermissionResultDeny

from text_util import sanitize_segment  # noqa: F401 — re-exported (existing
# callers/tests import `sanitize_segment` from this module; the canonical
# implementation now lives in text_util so plugin_store.py — stdlib-only,
# imported by the Dockerfile build helper before any venv — can share it
# without importing this (claude_agent_sdk-dependent) module.

logger = logging.getLogger(__name__)


def _mcp_servers(mcp_json_path: Path) -> dict:
    """The {server-name: config} map from a plugin ``.mcp.json`` — delegates to
    the shared, stdlib-only ``plugin_store.mcp_servers_map`` (also used by the
    build-time verifier) so both understand the wrapper AND top-level shapes."""
    from plugin_store import mcp_servers_map
    return mcp_servers_map(mcp_json_path)


def mcp_json_malformed(rp) -> bool:
    """Sol #16 / CI-review: True iff ``.mcp.json`` is PRESENT but broken — via the
    ONE shared parser (``plugin_store.parse_mcp_servers``) so it agrees with grant
    derivation across BOTH the ``mcpServers`` wrapper and the top-level shape:
    unparseable / not an object / non-mapping ``mcpServers`` / server-like objects
    none of which declare command|url. An ABSENT ``.mcp.json`` (skill-only) and an
    empty/no-server config are NOT malformed. Verify uses this so a broken MCP
    config can't report ready when grants/secrets degrade to ``[]``."""
    from plugin_store import parse_mcp_servers
    return parse_mcp_servers(Path(rp.path) / ".mcp.json")[1]


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


def declared_tools_for_resolution(res) -> set[str]:
    """Union of manifest-declared tool-level names for every resolved
    plugin (spec A5). ``grants_for_resolution`` is SERVER-level
    (``mcp__plugin_mtg_mtg``) — a ``requires.tools`` entry is TOOL-level
    (``mcp__plugin_mtg_mtg__lookup_rule``), so it must be checked against
    a manifest-declared inventory instead, never against server grants
    directly. Each plugin declares its own tools via
    ``casa.provides_tools: list[str]`` in its manifest — no "observe from
    grants" bootstrap; WS-B's plugin.json ships the names verbatim.

    FAIL CLOSED on malformed metadata (r1-review): a non-dict ``manifest``,
    a non-dict ``casa``, or a ``provides_tools`` that is not a list all
    contribute NOTHING, and only non-empty ``str`` entries survive from a
    valid list. This is deliberate — a dict ``provides_tools`` would
    otherwise leak its KEYS (a malformed manifest could then satisfy a tool
    requirement), and a non-list would raise a ``TypeError`` that escapes
    ``_prelaunch`` instead of denying with ``dependency_unavailable``.
    Malformed metadata degrades to "no declared tools", which makes the
    requirement unmet → ``dependency_unavailable`` (same never-raise
    contract as the other grant helpers in this module)."""
    out: set[str] = set()
    for rp in getattr(res, "plugins", None) or []:
        manifest = getattr(rp, "manifest", None)
        if not isinstance(manifest, dict):
            continue
        casa = manifest.get("casa")
        if not isinstance(casa, dict):
            continue
        provided = casa.get("provides_tools")
        if not isinstance(provided, list):
            if provided is not None:
                logger.debug(
                    "declared_tools_for_resolution: %s casa.provides_tools is "
                    "%s not a list — contributing nothing",
                    getattr(rp, "name", "?"), type(provided).__name__,
                )
            continue
        out.update(t for t in provided if isinstance(t, str) and t)
    return out


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


def protected_map(resolution) -> dict[str, dict]:
    """Full-tool-name -> ``{"artifact_id": str, "summary": str | None}`` map
    for every ``casa.protectedTools`` entry across a RESOLVED
    ``ResolutionResult`` (A:§3.7, value shape extended v0.78.0 W1). Derived
    from the RESOLVED ARTIFACT's manifest (the content-addressed store copy
    named by the ``ResolutionResult`` — never a duplicated registry field).
    ``summary`` is the plugin-declared advisory copy (``None`` for a legacy
    string entry or an object entry without one) — the authz hook consumes
    ``artifact_id`` exactly as before (NO grant/GrantKey/enforcement
    change); ``summary`` is threaded to the challenge render only (W2).

    Namespacing reuses the grant-derivation sanitization
    (``mcp__plugin_<plugin>_<server>__<tool>``); a BARE tool name expands
    across EVERY MCP server the plugin declares (a plugin with two servers
    protects the tool on both).

    PER-PLUGIN DEGRADATION (r2-B6/r3-4): a malformed ``casa.protectedTools``
    in one resolved plugin's manifest excludes JUST that plugin's protected
    tools from the map (logged at WARNING) — every other resolved plugin
    still contributes normally, matching the existing artifact-failure
    ``PluginIssue`` pattern. A plugin declaring no MCP servers (skill-only,
    or a malformed ``.mcp.json``) contributes nothing either, since there is
    no server to qualify the tool name with (no runtime MCP enumeration —
    B7).
    """
    from plugin_store import StoreError, manifest_protected_tools

    out: dict[str, dict] = {}
    for rp in getattr(resolution, "plugins", None) or []:
        try:
            entries = manifest_protected_tools(rp.manifest)
        except StoreError:
            logger.warning(
                "protected_tools_invalid: excluding %s (artifact_id=%s) "
                "from the protected-tool map", rp.name, rp.artifact_id)
            continue
        if not entries:
            continue
        servers = sorted(_mcp_servers(Path(rp.path) / ".mcp.json"))
        if not servers:
            continue
        plugin_seg = sanitize_segment(rp.name)
        for tool_entry in entries:
            tool_seg = sanitize_segment(tool_entry["name"])
            for server in servers:
                full = (f"mcp__plugin_{plugin_seg}_"
                        f"{sanitize_segment(server)}__{tool_seg}")
                out[full] = {"artifact_id": rp.artifact_id,
                             "summary": tool_entry["summary"]}
    return out


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
