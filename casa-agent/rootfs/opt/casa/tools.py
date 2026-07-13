"""In-process MCP tools for the Casa framework."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trigger_registry import TriggerRegistry

from executor_registry import ExecutorRegistry
from plugin_env_conf import set_entry as _set_env_entry  # noqa: F401 — available for future use
from system_requirements.orchestrator import install_requirements, OrchestrationError
from system_requirements.manifest import add_plugin_entry as add_manifest
import plugin_registry
import plugin_store
from plugin_grants import (
    grants_for_resolution, grants_for_resolved, make_fail_closed_can_use_tool,
    mcp_json_malformed, required_env_vars_for_resolved,
)
from delegated_memory import delegated_recall, retain_delegated

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from error_kinds import _classify_error
from mcp_registry import McpServerRegistry
import sdk_logging
from engagement_registry import EngagementRecord, EngagementRegistry
from specialist_registry import (
    DelegationComplete,
    DelegationRecord,
    SpecialistRegistry,
)

logger = logging.getLogger(__name__)

# Plan 4a.1 §8: workspace retention for claude_code driver engagements.
_ENGAGEMENTS_ROOT = "/data/engagements"
_WORKSPACE_RETENTION_DAYS = 7

# Module-level references, initialized via init_tools()
_channel_manager: ChannelManager | None = None
_bus: MessageBus | None = None
_specialist_registry: SpecialistRegistry | None = None
_mcp_registry: McpServerRegistry | None = None
_agent_role_map: dict[str, "AgentConfig"] = {}  # merged residents + specialists
_trigger_registry: "TriggerRegistry | None" = None
_engagement_registry: EngagementRegistry | None = None
_executor_registry: "ExecutorRegistry | None" = None
_agent_registry = None  # AgentRegistry | None
_runtime = None  # CasaRuntime | None — set by init_tools(runtime=...)
engagement_var: ContextVar[EngagementRecord | None] = ContextVar(
    "engagement_var", default=None,
)


def init_tools(
    channel_manager,
    bus,
    specialist_registry,
    mcp_registry=None,
    *,
    agent_role_map: dict | None = None,
    agent_registry=None,
    trigger_registry=None,
    engagement_registry=None,
    executor_registry=None,
    runtime=None,                         # NEW — Task C.1
) -> None:
    """Initialize module-level references used by tool implementations.

    ``mcp_registry`` is required for specialist MCP-tool resolution at
    delegation time. ``trigger_registry`` is required for the
    ``get_schedule`` tool; callers that don't pass it get a degraded
    tool that returns "not initialized" on every call.
    Accepts ``None`` for legacy callers that don't pass
    it (the `_build_specialist_options` code path degrades to empty MCP
    servers — the specialist still runs but with only built-in tools).

    ``agent_role_map`` is a merged dict of role→AgentConfig covering both
    residents and specialists. If omitted, ``delegate_to_agent`` falls back
    to resolving against ``specialist_registry`` alone (back-compat).

    ``runtime`` is the CasaRuntime container. Optional during migration
    (Task C.1); becomes required once all callsites use it (Task C.4).
    """
    global _channel_manager, _bus, _specialist_registry, _mcp_registry, \
        _agent_role_map, _agent_registry, _trigger_registry, \
        _engagement_registry, _executor_registry, _runtime  # noqa: PLW0603
    _channel_manager = channel_manager
    _bus = bus
    _specialist_registry = specialist_registry
    _mcp_registry = mcp_registry
    _agent_role_map = dict(agent_role_map or {})
    _agent_registry = agent_registry
    _trigger_registry = trigger_registry
    _engagement_registry = engagement_registry
    _executor_registry = executor_registry
    _runtime = runtime


def sync_agent_role_map(runtime: Any) -> None:
    """Rebuild the delegation role map from live runtime state.

    Called by the reload handlers after an agent/agents swap. Without
    this, the map stays a boot-time snapshot and ``delegate_to_agent``
    keeps resolving PRE-reload AgentConfigs — a specialist
    ``tools.allowed`` grant stays inert for every fresh delegation until
    a full add-on restart, even though ``casa_reload`` reports ok (P-6,
    live run 2026-07-11). Overlapping roles keep the resident entry and
    warn instead of raising: a reload must not brick on a collision
    boot would have rejected.
    """
    global _agent_role_map  # noqa: PLW0603
    residents = dict(getattr(runtime, "role_configs", {}) or {})
    registry = getattr(runtime, "specialist_registry", None)
    specialists = dict(registry.all_configs()) if registry is not None else {}
    merged = dict(residents)
    for name, cfg in specialists.items():
        if name in merged:
            logger.warning(
                "sync_agent_role_map: role %r exists in both tiers — "
                "resident entry wins", name,
            )
            continue
        merged[name] = cfg
    _agent_role_map = merged


@tool(
    "send_message",
    "Send a message to a user through a communication channel.",
    {"message": str, "channel": str},
)
async def send_message(args: dict) -> dict:
    """Send a message through a named channel."""
    message = args.get("message", "")
    channel = args.get("channel", "telegram")

    if _channel_manager is None:
        return {"content": [{"type": "text", "text": "Error: tools not initialized"}]}

    ch = _channel_manager.get(channel)
    if ch is None:
        return {"content": [{"type": "text", "text": f"Error: channel '{channel}' not found"}]}

    await ch.send(message, {})
    return {"content": [{"type": "text", "text": f"Message sent via {channel}."}]}


# ---------------------------------------------------------------------------
# delegate_to_agent — Phase 3.1
# ---------------------------------------------------------------------------


# Phase 3.1: sync-mode wait ceiling. 60 s per spec §6.3. Exposed as a
# module-level constant so tests can monkeypatch to drive the degraded
# path without waiting a minute.
_SYNC_WAIT_TIMEOUT_S: float = 60.0

# Phase 3.5 (Plan 4b): max delegation depth. depth=0 is a direct call from
# a resident; depth>=1 is a delegated turn. Cap at 1 to prevent chains.
_MAX_DELEGATION_DEPTH: int = 1


# G-2 hotfix (v0.33.1): defensive reload guard.
#
# v0.33.0's doctrine fix (invert canonical order to commit -> reload ->
# emit_completion) failed to converge live (verify cid `a9313680`
# 2026-05-01 11:39:57Z): model still skipped the reload tool_use after
# reading the new completion.md + reload.md and emitted the same false-
# positive narration ("Reload triggered to apply") without actually
# calling casa_reload. Exploration2 G-2 reproduced unchanged.
#
# Per kickoff: "Recommend doing the doctrine fix first; add the
# defensive guard only if doctrine fix doesn't converge after 2
# retries." Reverification confirmed non-convergence on the first
# active retry — we don't have budget to retry-and-pray, and the
# operator-visible failure mode (artifact COMMITTED BUT INERT) is
# severe.
#
# Mechanism: track per-engagement "did we still owe a reload at
# emit_completion time?" via a module-level set, populated by
# config_git_commit when the SHA points at a real commit, drained by
# the reload tools, and inspected at emit_completion entry. If still
# pending, force-call ``casa_reload`` (the safe-default — hard reload
# is always correct, just slower than soft for triggers-only changes)
# and emit a WARNING citing the engagement id.
_ENGAGEMENTS_PENDING_RELOAD: set[str] = set()

# H-1 fix (v0.34.0): casa_reload's Supervisor restart races the SDK
# subprocess — the POST returns in <1s but Supervisor's container kill
# arrives ~13s later, cancelling the SDK before the model can call
# emit_completion. Result: engagement stuck status=active, no user-DM
# completion message, _finalize_engagement never runs.
#
# Mechanism: when ``casa_reload`` is called inside an active engagement
# (engagement_var bound), it does NOT POST to Supervisor. Instead it
# adds engagement.id to this set and returns immediately. The actual
# Supervisor POST is performed at the end of _finalize_engagement —
# AFTER the bus-message write + engagement-summary retain land — so the
# user-DM "Done" relay survives the addon kill. Out-of-engagement
# casa_reload calls (operator-triggered via /invoke) still POST
# inline since there is no engagement to wait for.
_ENGAGEMENTS_DEFERRED_HARD_RELOAD: set[str] = set()


def _snapshot_origin() -> dict:
    """Copy the current origin at handler entry (AR-2, pooling spec §Q7).

    With a pooled SDK client, ``origin_var`` in the read-task context is
    bound to a MUTABLE holder rewritten at each turn start. Any handler
    that keeps the reference across an await that can outlive its turn
    (delegations most of all) would read the NEXT turn's origin — for
    the delegation retain gate that is a clearance violation, not just
    misattribution. Snapshot once, at entry, before any await."""
    import agent as agent_mod
    return dict(agent_mod.origin_var.get(None) or {})


def _result(payload: dict, *, is_error: bool | None = None) -> dict:
    """Wrap a JSON-serializable payload as the tool's MCP content.

    F-7 (v0.32.0): when ``payload["status"] == "error"`` (or the caller
    explicitly passes ``is_error=True``), set ``is_error: True`` on the
    envelope. Without this flag the SDK's ``ToolResultBlock`` defaults
    ``is_error=False`` and ``sdk_logging.log_tool_result`` emits
    ``ok=True`` even for failures — operators reading turn telemetry
    would think a registry-rejected ``engage_executor`` actually spawned.
    Auto-detection via ``payload["status"]`` keeps the existing call sites
    untouched while making every error path consistently observable.

    O-1 (v0.37.9): also recognise ``payload["ok"] is False`` so the
    install/uninstall plugin envelopes (which use ``{"ok": False,
    "error": ...}`` instead of ``{"status": "error", ...}``) surface as
    MCP errors too. Live evidence (2026-05-14 P29.1 cid ``52240634``):
    ``tool_result name=install_casa_plugin ok=True ms=12594`` for a
    ``plugin_not_in_marketplace`` failure — telemetry reported the
    failure as success, contradicting F-7's intent.

    The dict key MUST be ``is_error`` (snake_case): the Anthropic Agent
    SDK's MCP server adapter reads
    ``result.get("is_error", False)`` (see
    ``claude_agent_sdk/__init__.py:512``) and converts to the MCP wire
    field ``isError`` itself. Passing ``isError`` here gets silently
    dropped on the way to the model.
    """
    if is_error is None:
        is_error = (
            payload.get("status") == "error"
            or payload.get("ok") is False
        )
    envelope: dict = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if is_error:
        envelope["is_error"] = True
    return envelope


def _engagement_unavailable_result(origin: dict) -> dict:
    """R-2 (v0.69.7): the engagement supergroup/topic check failed — return an
    accurate error keyed on origin. A non-Telegram origin can't start an
    engagement at all (only the telegram channel carries the supergroup/topic
    machinery; full non-Telegram origination is backlogged), which is a
    different problem from a genuine telegram-side misconfiguration. The old
    single message ("set telegram_engagement_supergroup_id …") misdiagnosed the
    non-Telegram case as an add-on config gap."""
    origin_channel = origin.get("channel", "telegram")
    if origin_channel != "telegram":
        return _result({
            "status": "error", "kind": "engagement_wrong_origin",
            "message": (
                "engagements can only be initiated from Telegram; this request "
                f"originated from {origin_channel!r}. Non-Telegram origination "
                "is not supported yet."
            ),
        })
    return _result({
        "status": "error", "kind": "engagement_not_configured",
        "message": ("set telegram_engagement_supergroup_id in addon options "
                    "and verify the bot has can_manage_topics"),
    })


# Q-1 (v0.69.8, operator decision 2026-07-12): the SDK meta-tools that spawn a
# sub-agent. They bypass `allowed_tools` AND the v0.68.0 fail-closed
# can_use_tool callback (empirically: the CLI does not consult the callback for
# them), so a restricted agent could spawn a sub-agent that reaches a broad
# default toolset its own allowlist excludes. `disallowed_tools` IS enforced by
# the CLI (removes them from the surface), so specialists — and butler, via its
# runtime.yaml — are denied these. NOT `ToolSearch` (operator kept it: it is
# the deferred-tool-load mechanism and cannot spawn a sub-agent on its own).
_SUBAGENT_SPAWN_TOOLS = ("Agent", "Task")


def _with_subagent_spawn_disallowed(disallowed) -> list[str]:
    """Return ``disallowed`` (any iterable) plus the sub-agent-spawn tools,
    de-duplicated, order-stable."""
    out = list(disallowed)
    for t in _SUBAGENT_SPAWN_TOOLS:
        if t not in out:
            out.append(t)
    return out


def _build_specialist_options(cfg) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a Tier 2 specialist invocation.

    Specialist memory is injected via prompt in :func:`_run_delegated_agent`
    (shared ``casa`` bank); SDK-level session resume stays disabled
    (``resume=None``) — memory enters via prompt injection, not SDK
    continuity. Hooks are
    resolved from the specialist's own ``cfg.hooks``. MCP servers are
    resolved via the shared registry — same pattern as
    :meth:`Agent._process` (agent.py step 4). Degrades to empty-dict
    when the registry is not bound (legacy callers / test harnesses)."""
    from hooks import resolve_hooks

    if _mcp_registry is not None:
        mcp_servers = _mcp_registry.resolve(cfg.mcp_server_names)
    else:
        mcp_servers = {}

    resolved_hooks = resolve_hooks(cfg.hooks, default_cwd=cfg.cwd)
    # Sol #5: inject the /config/plugins + settings.json guard code-side (like
    # residents — agent.py step 5). A delegated specialist with Bash could
    # otherwise `echo > /config/plugins/registry.json`, bypassing validation and
    # §3.9 sequencing. Fresh dict so the shared resolved hooks aren't mutated.
    from hooks import agent_home_settings_guard_matcher
    resolved_hooks = dict(resolved_hooks)
    resolved_hooks["PreToolUse"] = [
        *resolved_hooks.get("PreToolUse", []),
        agent_home_settings_guard_matcher(),
    ]

    # Unified plugin architecture (§3.3): resolve with the CONCRETE tier + role.
    # Sol #12: delegate_to_agent also routes RESIDENTS through this builder
    # (sync/async delegation of e.g. butler), so a hardcoded "specialist:"
    # dropped a delegated resident's resident:<role> plugins. Use the
    # authoritative AgentRegistry tier; fall back to specialist for unknown roles.
    _role = getattr(cfg, "role", "unknown")
    _tier = (_agent_registry.tier_for_role(_role)
             if _agent_registry is not None else None) or "specialist"
    resolution = plugin_registry.resolve_for(f"{_tier}:{_role}")
    sdk_plugins = [{"type": "local", "path": rp.path}
                   for rp in resolution.plugins]

    agent_home = (cfg.cwd
                  or f"/config/agent-home/{getattr(cfg, 'role', 'unknown')}")

    # Skills via skills="all" below; strip any config-supplied "Skill"
    # (deprecated) — (f) v0.69.9.
    allowed_tools = [t for t in cfg.tools.allowed if t != "Skill"]
    # P-5a: installed ⇒ granted, by construction. Server-level grants from the
    # SAME resolved artifacts; disallowed_tools still wins at the CC layer.
    for grant in grants_for_resolution(resolution):
        if grant not in allowed_tools:
            allowed_tools.append(grant)

    return ClaudeAgentOptions(
        model=cfg.model,
        system_prompt=cfg.system_prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=_with_subagent_spawn_disallowed(cfg.tools.disallowed),
        permission_mode=cfg.tools.permission_mode or "acceptEdits",
        max_turns=cfg.tools.max_turns,
        mcp_servers=mcp_servers if mcp_servers else {},
        hooks=resolved_hooks,
        cwd=agent_home,
        resume=None,
        setting_sources=["project"],
        skills="all",  # (f) v0.69.9
        plugins=sdk_plugins,
        # P-5b: no relay exists on this path — deny ungranted tools fast
        # instead of hanging on an unanswerable CC prompt.
        can_use_tool=make_fail_closed_can_use_tool(
            getattr(cfg, "role", "unknown")),
    )


def _build_executor_options(defn, *, executor_type: str,
                            resolution=None,
                            plugin_paths: "list[str] | None" = None,
                            ) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a Tier 3 Executor invocation.

    Unlike specialists, executors DO have MCP servers and structured hooks
    driven by their definition.yaml + hooks.yaml. Prompt is injected at
    engage_executor time - this helper does not set system_prompt.

    Plugins (§3.8/§3.9), in priority order: explicit ``plugin_paths`` (resume
    from recorded artifacts — never re-resolved), else a passed ``resolution``
    (engage_executor feeds the SAME result it gated + recorded — one resolve,
    one binding), else a fresh resolve for ``executor:<executor_type>``.
    Executors get NO grant merge + NO can_use_tool (they keep the relay).
    """
    from config import HooksConfig
    from hooks import resolve_hooks
    import yaml

    hooks_cfg = HooksConfig()
    if defn.hooks_path and os.path.isfile(defn.hooks_path):
        with open(defn.hooks_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        hooks_cfg = HooksConfig(pre_tool_use=list(raw.get("pre_tool_use") or []))

    resolved_hooks = resolve_hooks(hooks_cfg, default_cwd="/config")
    # Sol #5: in_casa executors (e.g. configurator, cwd=/config, Bash allowed)
    # could `echo > /config/plugins/registry.json` — inject the same code-side
    # guard so a Bash write to /config/plugins or settings.json is denied.
    from hooks import agent_home_settings_guard_matcher
    resolved_hooks = dict(resolved_hooks)
    resolved_hooks["PreToolUse"] = [
        *resolved_hooks.get("PreToolUse", []),
        agent_home_settings_guard_matcher(),
    ]

    if _mcp_registry is not None:
        mcp_servers = _mcp_registry.resolve(defn.mcp_server_names)
    else:
        mcp_servers = {}

    if plugin_paths is not None:
        sdk_plugins = [{"type": "local", "path": p} for p in plugin_paths]
    else:
        if resolution is None:
            resolution = plugin_registry.resolve_for(
                f"executor:{executor_type}")
        sdk_plugins = [{"type": "local", "path": rp.path}
                       for rp in resolution.plugins]

    # Skills via skills="all" below; strip any config-supplied "Skill"
    # (deprecated) — (f) v0.69.9.
    allowed_tools = [t for t in defn.tools_allowed if t != "Skill"]

    # Executors (in_casa driver — Configurator, future Tier-3) operate on
    # the addon-config root rather than an agent-home, because their
    # mutation surface spans /config/ (agents/, marketplace/,
    # plugin-env.conf, etc.).
    return ClaudeAgentOptions(
        model=defn.model,
        system_prompt="",
        allowed_tools=allowed_tools,
        disallowed_tools=list(defn.tools_disallowed),
        permission_mode=defn.permission_mode or "acceptEdits",
        max_turns=200,
        mcp_servers=mcp_servers if mcp_servers else {},
        hooks=resolved_hooks,
        cwd="/config",
        resume=None,
        setting_sources=["project"],
        skills="all",  # (f) v0.69.9
        plugins=sdk_plugins,
    )


def build_engagement_resume_options(engagement, session_id: str) -> ClaudeAgentOptions:
    """Rebuild the FULL option set for a resumed engagement, then attach
    ``resume=session_id`` (Finding 2, codex review v0.69.10).

    ``InCasaDriver.resume()`` used to open a BARE ``ClaudeAgentOptions(resume=)``,
    so a resumed interactive specialist/executor lost ``disallowed_tools``
    (Agent/Task — Q-1), the fail-closed ``can_use_tool`` callback, ``hooks``
    (the I-2 settings guard), ``skills``, ``mcp_servers``, and ``cwd`` — running
    on the CLI's broad default surface. Rebuilt from the engagement's CURRENT
    config via the same builder the initial start used.

    Fails CLOSED: if the specialist/executor config is gone (removed while the
    engagement was suspended), raise rather than resume with dropped
    restrictions. §3.8: an executor resumes from its RECORDED plugin artifacts
    (never re-resolving current assignments); a missing recorded artifact
    fails the resume closed (plugin_artifact_missing). Reads registry snapshot
    files — call off the event loop.
    """
    import dataclasses

    kind = getattr(engagement, "kind", "")
    role = getattr(engagement, "role_or_type", "")
    opts: ClaudeAgentOptions | None = None
    if kind == "executor":
        defn = _executor_registry.get(role) if _executor_registry is not None else None
        if defn is not None:
            recorded = getattr(engagement, "plugin_artifacts", None) or ()
            plugin_paths: list[str] = []
            for pa in recorded:
                path = pa.get("path") if isinstance(pa, dict) else None
                if not path or not os.path.isdir(path):
                    raise RuntimeError(
                        f"cannot resume executor engagement role={role!r}: "
                        f"recorded plugin artifact missing "
                        f"(plugin_artifact_missing): {pa!r}")
                plugin_paths.append(path)
            opts = _build_executor_options(defn, executor_type=role,
                                           plugin_paths=plugin_paths)
    else:
        cfg = _specialist_registry.get(role) if _specialist_registry is not None else None
        if cfg is not None:
            opts = _build_specialist_options(cfg)
    if opts is None:
        raise RuntimeError(
            f"cannot rebuild options to resume {kind or 'specialist'} engagement "
            f"role={role!r}: config not found (fail-closed — refusing a bare "
            "resume that would drop Agent/Task denies + the fail-closed callback)"
        )
    return dataclasses.replace(opts, resume=session_id)


def _build_world_state_summary() -> str:
    """Return a short (<=500 tokens) snapshot of Casa's config surface.

    Called at engagement start and interpolated into the executor's
    prompt template as {world_state_summary}. Read-only - does not
    include in-flight session or delegation state.
    """
    lines: list[str] = []
    try:
        specialists = sorted(
            getattr(_specialist_registry, "_configs", {}).keys()
        ) if _specialist_registry else []
    except Exception:  # noqa: BLE001
        specialists = []
    lines.append(f"Enabled specialists:  {', '.join(specialists) or '(none)'}")

    residents: list[str] = []
    agents_dir = "/config/agents"
    try:
        if os.path.isdir(agents_dir):
            for name in sorted(os.listdir(agents_dir)):
                if name in ("specialists", "executors"):
                    continue
                if os.path.isdir(os.path.join(agents_dir, name)):
                    residents.append(name)
    except OSError:
        pass
    lines.append(f"Residents:            {', '.join(residents) or '(none)'}")

    try:
        import agent as agent_mod
        exec_reg = getattr(agent_mod, "active_executor_registry", None)
        exec_types = exec_reg.list_types() if exec_reg else []
    except Exception:  # noqa: BLE001
        exec_types = []
    lines.append(f"Enabled executors:    {', '.join(exec_types) or '(none)'}")

    version = "unknown"
    for candidate in ("/opt/casa/VERSION", "/config/VERSION"):
        try:
            with open(candidate) as fh:
                version = fh.read().strip()
                break
        except OSError:
            continue
    lines.append(f"Addon version:        {version}")

    return "\n".join(lines)


# Specialist memory write-path bg-task anchoring (parity with Agent._bg_tasks
# at agent.py:133). Module-level so it persists across delegate_to_agent calls.
_specialist_bg_tasks: set[asyncio.Task[Any]] = set()


async def _run_delegated_agent(cfg, task_text: str, context_text: str) -> str:
    """Run one ephemeral delegated turn and return the concatenated text."""
    import agent as agent_mod
    # AR-2: snapshot BEFORE any await — this coroutine outlives the parent
    # turn (async delegations especially), and a pooled client's origin_var
    # holder can be rewritten by the NEXT turn while this one is in flight.
    parent = _snapshot_origin()
    child_origin = {
        **parent,
        "delegation_depth": int(parent.get("delegation_depth", 0)) + 1,
    }

    # Resolve caller display name; fall back to role.
    caller_role = str(parent.get("role", "")) or "(unknown)"
    caller_name = (
        _agent_registry.role_to_name(caller_role)
        if _agent_registry is not None else caller_role
    )
    originating_channel = str(parent.get("channel", "")) or "(unknown)"
    suggested_register = "voice" if originating_channel == "voice" else "text"

    delegation_context = (
        "<delegation_context>\n"
        f"caller_role: {caller_role}\n"
        f"caller_name: {caller_name}\n"
        f"originating_channel: {originating_channel}\n"
        f"suggested_register: {suggested_register}\n"
        "</delegation_context>"
    )

    if context_text:
        body_tail = (
            f"Task: {task_text}\n\n"
            f"Context from {caller_name}:\n{context_text}"
        )
    else:
        body_tail = f"Task: {task_text}"

    # Specialist memory read on the shared `casa` bank, at the PARENT context's
    # read-clearance (design §3, plan 3). Opt-in via cfg.memory.token_budget > 0.
    memory_block = ""
    if cfg.memory.token_budget > 0:
        sem = getattr(agent_mod, "active_semantic_memory", None)
        if sem is not None:
            digest = await delegated_recall(
                sem, query=task_text,
                origin_channel=str(parent.get("channel", "")),
                max_tokens=cfg.memory.token_budget,
            )
            if digest:
                memory_block = (
                    f'<memory_context agent="{cfg.role}">\n'
                    f"{digest}\n"
                    f"</memory_context>\n\n"
                )

    prompt = f"{delegation_context}\n\n{memory_block}{body_tail}"

    # Off-loop: _build_specialist_options resolves the registry (file IO) —
    # keep it off the shared event loop (H2/M20).
    # Sol #4 residual (documented): this ephemeral delegation client pins the
    # artifacts it resolves here for its (short) lifetime; it is NOT recorded in
    # runtime.agents or the engagement registry, so a plugin_update landing
    # mid-delegation is not disclosed by verify until the delegation ends. This
    # window is transient and self-healing (the next delegation resolves the new
    # artifact); the PERSISTENT stale-binding incident is fully closed. Tracked
    # for a future live-binding registry (docs/ROADMAP-backlog.md).
    options = await asyncio.to_thread(_build_specialist_options, cfg)
    text = ""
    token = agent_mod.origin_var.set(child_origin)
    try:
        async with ClaudeSDKClient(
            sdk_logging.with_stderr_callback(options, engagement_id=None),
        ) as client:
            await client.query(prompt)
            async for sdk_msg in client.receive_response():
                if isinstance(sdk_msg, AssistantMessage):
                    for block in getattr(sdk_msg, "content", []):
                        if isinstance(block, TextBlock):
                            text += block.text
    finally:
        agent_mod.origin_var.reset(token)

    # Specialist write: one explicit tier-classified retain of the exchange to
    # the shared bank, gated by the PARENT channel's write-trust (voice → no
    # write) — design §3, plan 3. Ephemeral specialists have no session
    # registry, so the freshness reaper never sees them; the retain is explicit.
    if cfg.memory.token_budget > 0 and text:
        sem = getattr(agent_mod, "active_semantic_memory", None)
        if sem is not None:
            cid = str(parent.get("cid", "-"))
            bg = asyncio.create_task(retain_delegated(
                sem, origin_channel=str(parent.get("channel", "")),
                doc_prefix=f"delegation:{cid}:{cfg.role}",
                turns=[("user", task_text), ("assistant", text)],
            ))
            _specialist_bg_tasks.add(bg)
            bg.add_done_callback(_specialist_bg_tasks.discard)

    return text


def _attach_completion_callback(
    task: asyncio.Task,
    record: DelegationRecord,
) -> None:
    """Wire the bus NOTIFICATION post on delegation completion.

    Used by the degraded-sync and async paths. Task 7's sync-ok /
    sync-error paths bookkeep inline.
    """
    loop = asyncio.get_running_loop()

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            loop.create_task(_specialist_registry.cancel_delegation(record.id))
            return
        complete: DelegationComplete | None = None
        try:
            text = t.result()
            complete = DelegationComplete(
                delegation_id=record.id,
                agent=record.agent,
                status="ok",
                text=text,
                origin=record.origin,
                elapsed_s=time.time() - record.started_at,
            )
            loop.create_task(_specialist_registry.complete_delegation(record.id))
        except Exception as exc:
            kind = _classify_error(exc).value
            complete = DelegationComplete(
                delegation_id=record.id,
                agent=record.agent,
                status="error",
                kind=kind,
                message=str(exc),
                origin=record.origin,
                elapsed_s=time.time() - record.started_at,
            )
            loop.create_task(_specialist_registry.fail_delegation(record.id, exc))

        if _bus is None or complete is None:
            return
        target_role = record.origin.get("role") or "assistant"
        loop.create_task(_bus.notify(BusMessage(
            type=MessageType.NOTIFICATION,
            source=record.agent,
            target=target_role,
            content=complete,
            channel=record.origin.get("channel", ""),
            context={
                "cid": record.origin.get("cid", "-"),
                "chat_id": record.origin.get("chat_id", ""),
                "delegation_id": record.id,
            },
        )))
    task.add_done_callback(_done)


@tool(
    "delegate_to_agent",
    "Delegate a task to another agent (resident or specialist) and return its result.",
    {"agent": str, "task": str, "context": str, "mode": str},
)
async def delegate_to_agent(args: dict) -> dict:
    """Invoke a Tier 2 specialist via the SDK and return its text.

    Sync mode (default): ``asyncio.wait`` up to 60s, return ok/error
    content; on timeout, attach completion callback and return a
    ``pending`` marker so the delegating resident can narrate
    "still working" and move on.

    Async mode (``mode="async"``): skip the wait, attach callback,
    return ``pending`` immediately.
    """
    # Import lazily — matches the `agent.py` origin_var ContextVar.
    import agent as agent_mod

    agent_name = args.get("agent", "")
    task_text = args.get("task", "")
    context_text = args.get("context", "") or ""
    mode = args.get("mode", "sync") or "sync"

    if _specialist_registry is None:
        return _result({
            "status": "error",
            "kind": "not_initialized",
            "message": "specialist registry not initialized",
        })

    # Check origin BEFORE agent lookup: the tool must never dispatch
    # without an origin, even if the name is also invalid. Lets
    # callers test the no-origin branch without first seeding a
    # valid specialist.
    # AR-2: snapshot at entry — this handler awaits (channel setup,
    # engagement/delegation dispatch) and must not read a holder that a
    # later turn has since rewritten in place.
    origin = _snapshot_origin()
    if not origin:
        return _result({
            "status": "error",
            "kind": "no_origin",
            "message": "delegate_to_agent called outside a turn",
        })

    # Check depth cap: prevent delegation chains beyond depth=1.
    current_depth = int((origin or {}).get("delegation_depth", 0))
    if current_depth >= _MAX_DELEGATION_DEPTH:
        return _result({
            "status": "error",
            "kind": "delegation_depth_exceeded",
            "message": (
                f"Delegation depth {current_depth} exceeds cap "
                f"{_MAX_DELEGATION_DEPTH}; cannot chain further."
            ),
        })

    # Resolve target. Look in the merged role map (residents + specialists)
    # first; fall back to the specialist registry for back-compat with any
    # caller still relying on the old wiring.
    cfg = _agent_role_map.get(agent_name) or (
        _specialist_registry.get(agent_name)
        if _specialist_registry is not None else None
    )
    if cfg is None:
        return _result({
            "status": "error",
            "kind": "unknown_agent",
            "message": f"No enabled agent named {agent_name!r}",
        })

    is_resident = bool(getattr(cfg, "channels", []))
    if mode == "interactive" and is_resident:
        return _result({
            "status": "error",
            "kind": "interactive_not_supported",
            "message": (
                f"Cannot open a Telegram engagement for resident "
                f"{agent_name!r} — residents already own their own channels."
            ),
        })

    if mode == "interactive":
        # Need telegram channel + supergroup configured.
        if _channel_manager is None:
            return _result({"status": "error", "kind": "no_channel_manager",
                            "message": "channel manager missing"})
        channel = _channel_manager.get(origin.get("channel", "telegram"))
        # E-F (v0.30.0): if supergroup IS configured but
        # engagement_permission_ok is still False, the boot-time setup may
        # have lost a race with a transient network blip. The setup is now
        # wired into _rebuild's tail (self-healing on every reconnect), but
        # in the rare window where the user spawns an engagement before any
        # rebuild has completed, attempt one in-line retry before giving up.
        # Idempotent; cheap on success.
        if (channel is not None
                and getattr(channel, "engagement_supergroup_id", 0)
                and not getattr(channel, "engagement_permission_ok", False)):
            try:
                await channel.setup_engagement_features()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "engage_executor: in-line setup_engagement_features "
                    "retry failed: %s", exc,
                )
        if (channel is None
                or not getattr(channel, "engagement_supergroup_id", 0)
                or not getattr(channel, "engagement_permission_ok", False)):
            return _engagement_unavailable_result(origin)  # R-2 (v0.69.7)
        # v0.37.1 D-1: U3 title format for specialist engagements too
        # (was legacy `#[<role>] <task> · <id8>`). Bubble carries the
        # role icon via icon_id_for_role; title is `<state> <task>`.
        from channels.state_emoji import (
            STATE_EMOJI, compose_topic_title, concise_task,
        )
        first_line = (task_text or "").splitlines()[0]
        short_task = concise_task(first_line) or "engagement"
        topic_name = compose_topic_title(
            state="active", short_task=short_task,
        )
        try:
            topic_id = await channel.open_engagement_topic(
                name=topic_name,
                role=agent_name,
            )
        except Exception as exc:  # noqa: BLE001
            return _result({"status": "error", "kind": "topic_create_failed",
                            "message": str(exc)})
        # §3.8 (Sol #4): record the specialist's plugin binding so verify can
        # disclose this engagement if a later plugin_update supersedes its
        # artifact (informational — mirrors the executor-engagement case). The
        # specialist runs on the same tier:role resolution _build_specialist_
        # options uses below.
        _spec_tier = (_agent_registry.tier_for_role(agent_name)
                      if _agent_registry is not None else None) or "specialist"
        _spec_arts = tuple(
            {"name": rp.name, "artifact_id": rp.artifact_id, "path": rp.path}
            for rp in plugin_registry.resolve_for(
                f"{_spec_tier}:{agent_name}").plugins)
        # Create record
        rec = await _engagement_registry.create(
            kind="specialist", role_or_type=agent_name, driver="in_casa",
            task=task_text, origin=dict(origin), topic_id=topic_id,
            plugin_artifacts=_spec_arts,
        )
        # Persist initial state emoji so update_topic_state knows
        # whether it needs to edit the title (no-op when state didn't change).
        try:
            await _engagement_registry.set_channel_state(
                rec.id, current_state_emoji=STATE_EMOJI["active"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_channel_state(active) failed: %s", exc)

        # Build options + start driver (off-loop: registry resolve is file IO).
        options = await asyncio.to_thread(_build_specialist_options, cfg)
        # Augment allowed tools (additive) with query_engager + emit_completion
        injected = list(options.allowed_tools or [])
        for t in ("mcp__casa-framework__query_engager",
                  "mcp__casa-framework__emit_completion"):
            if t not in injected:
                injected.append(t)
        options.allowed_tools = injected

        prompt = (
            f"You are engaged with the user in a Telegram forum topic.\n"
            f"Task: {task_text}\n\n"
            f"Context from Ellen:\n{context_text or '(none)'}\n\n"
            f"When the task is complete, call emit_completion(text=..., "
            f"artifacts=..., next_steps=..., status='ok')."
        )

        driver = getattr(agent_mod, "active_engagement_driver", None)
        if driver is None:
            await _engagement_registry.mark_error(
                rec.id, kind="no_driver",
                message="engagement driver not initialized",
            )
            await _abort_engagement_topic(channel, rec.id, topic_id)
            return _result({"status": "error", "kind": "no_driver",
                            "message": "engagement driver not initialized"})
        try:
            await driver.start(rec, prompt=prompt, options=options)
        except Exception as exc:  # noqa: BLE001
            await _engagement_registry.mark_error(rec.id, kind="driver_start_failed",
                                                  message=str(exc))
            await _abort_engagement_topic(channel, rec.id, topic_id)
            return _result({"status": "error", "kind": "driver_start_failed",
                            "message": str(exc)})

        return _result({
            "status": "pending",
            "engagement_id": rec.id,
            "agent": agent_name,
            "mode": "interactive",
            "topic_id": topic_id,
        })

    delegation_id = str(uuid.uuid4())
    started_at = time.time()
    record = DelegationRecord(
        id=delegation_id, agent=agent_name, started_at=started_at,
        origin=dict(origin),
    )
    await _specialist_registry.register_delegation(record)

    task = asyncio.create_task(_run_delegated_agent(cfg, task_text, context_text))

    if mode == "async":
        _attach_completion_callback(task, record)
        logger.info(
            "Delegation %s → %s (async mode)",
            delegation_id[:8], agent_name,
        )
        return _result({
            "status": "pending",
            "delegation_id": delegation_id,
            "agent": agent_name,
            "mode": "async",
        })

    # mode == "sync"
    try:
        done, pending = await asyncio.wait({task}, timeout=_SYNC_WAIT_TIMEOUT_S)
    except asyncio.CancelledError:
        task.cancel()
        await _specialist_registry.cancel_delegation(delegation_id)
        raise

    if pending:
        # 60s elapsed; detach and degrade to pending with callback.
        _attach_completion_callback(task, record)
        logger.info(
            "Delegation %s → %s timed out at 60s — degraded to pending",
            delegation_id[:8], agent_name,
        )
        return _result({
            "status": "pending",
            "delegation_id": delegation_id,
            "agent": agent_name,
            "timeout_s": 60,
            "note": (
                "Delegation continues in background; you will receive a "
                "NOTIFICATION when complete."
            ),
        })

    # Task finished within 60s — return ok or error synchronously.
    finished = next(iter(done))
    if finished.exception() is not None:
        exc = finished.exception()
        kind = _classify_error(exc).value
        await _specialist_registry.fail_delegation(delegation_id, exc)
        elapsed = time.time() - started_at
        logger.info(
            "Delegation %s → %s failed: %s (%s)",
            delegation_id[:8], agent_name, kind, exc,
        )
        return _result({
            "status": "error",
            "delegation_id": delegation_id,
            "agent": agent_name,
            "kind": kind,
            "message": str(exc),
            "elapsed_s": elapsed,
        })

    text = finished.result()
    await _specialist_registry.complete_delegation(delegation_id)
    elapsed = time.time() - started_at
    logger.info(
        "Delegation %s → %s ok (%.2fs)",
        delegation_id[:8], agent_name, elapsed,
    )
    return _result({
        "status": "ok",
        "delegation_id": delegation_id,
        "agent": agent_name,
        "elapsed_s": elapsed,
        "text": text,
    })


# ---------------------------------------------------------------------------
# recall_memory — spec §4.3
# ---------------------------------------------------------------------------


@tool(
    "recall_memory",
    "Search your long-term memory for facts relevant to a query.",
    {"query": str},
)
async def recall_memory(args: dict) -> dict:
    """On-demand semantic recall against the shared 'casa' bank, filtered by the channel's tier clearance (spec §4.3).
    Voice uses budget=low so the rerank never stalls the turn."""
    import agent as agent_mod

    query = (args.get("query") or "").strip()
    if not query:
        return _result({"status": "error", "kind": "empty_query",
                        "message": "Error: query is required"})
    sem = getattr(agent_mod, "active_semantic_memory", None)
    if sem is None:
        return _result({"status": "ok", "memory": ""})  # not wired / cold

    origin = _snapshot_origin()
    role = origin.get("role", "assistant")
    channel = origin.get("channel", "telegram")
    caller_cfg = _agent_role_map.get(role)

    # Tier clearance — the same read-side gate the turn path uses (design §2.3).
    from sensitivity import clearance_for_channel, readable_tiers
    tags = readable_tiers(clearance_for_channel(channel))

    budget = "low" if channel == "voice" else "mid"
    tokens = (
        getattr(getattr(caller_cfg, "memory", None), "token_budget", 2000)
        if caller_cfg else 2000
    )
    from hindsight_ids import bank_id
    try:
        digest = await sem.recall(
            bank_id("casa"), query,
            tags=tags, max_tokens=tokens, budget=budget,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("recall_memory failed for role=%r: %s", role, exc)
        digest = ""
    return _result({"status": "ok", "memory": digest})


# ---------------------------------------------------------------------------
# get_schedule — Phase 3.3
# ---------------------------------------------------------------------------


@tool(
    "get_schedule",
    "Return your upcoming scheduled triggers (interval + cron) within a "
    "time window. Returns a markdown bullet list with name, type, cron/interval "
    "description, and next fire time. Own-role only.",
    {"within_hours": int},
)
async def get_schedule(args: dict) -> dict:
    if _trigger_registry is None:
        return {"content": [{"type": "text",
                             "text": "Error: trigger registry not initialized"}]}

    origin = _snapshot_origin()
    if not origin:
        return {"content": [{"type": "text",
                             "text": "Error: get_schedule called outside a turn context"}]}

    role = origin.get("role") or ""
    if not role:
        return {"content": [{"type": "text",
                             "text": "Error: turn origin has no role"}]}

    raw_hours = args.get("within_hours", 24)
    try:
        within_hours = int(raw_hours) if raw_hours is not None else 24
    except (TypeError, ValueError):
        within_hours = 24
    within_hours = max(1, min(720, within_hours))

    summaries = _trigger_registry.list_jobs_for(
        role=role, within_hours=within_hours,
    )

    if not summaries:
        text = f"(no scheduled triggers in the next {within_hours} hours)"
    else:
        lines = []
        for s in summaries:
            if s.type == "cron":
                desc = f"(cron, `{s.schedule_desc}`)"
            else:
                desc = f"(interval, {s.schedule_desc})"
            lines.append(
                f"- **{s.name}** {desc} — next: "
                f"{s.next_fire.isoformat(timespec='seconds')}"
            )
        text = "\n".join(lines)

    return {"content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# config_git_commit - Plan 3 (Tier 3 executor support)
# ---------------------------------------------------------------------------


# Bug 7 (v0.14.6): role guard for the privileged config tools.
# Pre-fix: gated only by each agent's runtime.yaml::tools.allowed,
# meaning a copy-paste error or permissive default in a new resident /
# specialist / executor silently exposed addon-restart and config-commit
# powers. Defense in depth at the tool itself.
_PRIVILEGED_CONFIG_ROLES = frozenset({"configurator"})


def _effective_caller_role() -> str | None:
    """Return the calling agent's role for authorisation checks.

    Inside an active engagement (engagement_var set), the calling role
    IS the engagement's role_or_type — this takes precedence over the
    bus's origin_var.role, which inside in_casa engagements still
    reflects the engager (Ellen's "assistant") because contextvars
    inherit through the same async task.

    Returns None if neither context is bound — caller must refuse rather
    than fall back to a permissive default.
    """
    eng = engagement_var.get(None)
    if eng is not None:
        r = getattr(eng, "role_or_type", None)
        if r:
            return r
    try:
        origin = _snapshot_origin()
        if origin:
            r = origin.get("role")
            if r:
                return r
    except Exception:  # noqa: BLE001 - defensive against import-time issues
        pass
    return None


def _refuse_unprivileged(tool_name: str, caller: str | None) -> dict:
    return _result({
        "status": "error",
        "kind": "not_authorized",
        "message": (
            f"{tool_name} is restricted to roles "
            f"{sorted(_PRIVILEGED_CONFIG_ROLES)}; calling role={caller!r}. "
            "Have a configurator engagement perform this action instead."
        ),
    })


@tool(
    "config_git_commit",
    "Stage and commit all tracked changes under /config/ (tracked: agents/, "
    "policies/, schema/, plugins/registry.json; everything else incl. "
    "plugins/store/, plugins/.staging/, and plugin-env.conf is gitignored). "
    "Returns the commit SHA — empty plus a warning when nothing tracked "
    "changed, which is the expected outcome for gitignored-only writes. "
    "Restricted to the configurator executor role.",
    {"message": str},
)
async def config_git_commit(args: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("config_git_commit", caller)

    message = args.get("message") or "configurator: commit"
    config_dir = "/config"
    try:
        import config_git
        import agent_loader

        # E-G (v0.31.0): pre-commit schema-validation gate. Refuse the
        # commit if any schema-bearing YAML in the repo would fail
        # boot-time agent_loader validation. Without this, the
        # configurator can write structurally-valid but schema-invalid
        # YAML (e.g., the v0.30.0 ``TRAIT:`` top-level-key repro) and
        # the addon FATALs on next boot. See
        # ``project_eg_configurator_schema_invalid_yaml`` and
        # ``docs/bug-review-2026-05-01-exploration.md`` for the
        # exploration-session repro.
        errors = await asyncio.to_thread(
            agent_loader.validate_config_repo, config_dir,
        )
        if errors:
            return _result({
                "status": "error",
                "kind": "schema_invalid",
                "message": (
                    f"Refusing commit: {len(errors)} schema validation "
                    f"failure(s). Fix the offending YAML and retry."
                ),
                "errors": errors,
            })

        sha = await asyncio.to_thread(
            config_git.commit_config, config_dir, message,
        )
        # G-2 hotfix (v0.33.1): mark this engagement as needing a
        # reload before emit_completion. Drained by casa_reload /
        # casa_reload_triggers; force-honored by emit_completion.
        # `sha` is empty string when nothing actually changed (no-op
        # commit) — only register the pending state when a real commit
        # landed.
        if sha:
            eng = engagement_var.get(None)
            if eng is not None:
                _ENGAGEMENTS_PENDING_RELOAD.add(eng.id)
            return _result({"sha": sha, "message": message})
        # P-3 (v0.69.1): a bare {"sha": ""} left agents looping to reconcile
        # "committed ok" against "file still untracked" when their writes
        # landed on gitignored paths. Say it loudly instead.
        return _result({
            "sha": "", "message": message,
            "warning": (
                "No tracked changes to commit. The config repo tracks ONLY "
                "agents/, policies/, schema/ and plugins/registry.json; every "
                "other path is gitignored by design — plugins/store/, "
                "plugins/.staging/, and plugin-env.conf (a secrets file) must "
                "never enter git history. If you only wrote gitignored paths, "
                "an empty SHA is the expected, correct outcome: report it as "
                "such and do NOT retry the commit."
            ),
        })
    except Exception as exc:  # noqa: BLE001
        return _result({
            "status": "error",
            "kind": "git_error",
            "message": str(exc),
        })


# ---------------------------------------------------------------------------
# casa_reload - Plan 3 (hard reload via Supervisor addon restart)
# ---------------------------------------------------------------------------


@tool(
    "casa_reload",
    "In-process reload of Casa runtime state at a given scope. "
    "Valid scopes: 'agent' (requires role), 'triggers' (requires role), "
    "'policies', 'plugin_env', 'agents', 'executors', 'config_sync', 'full'. Use 'full' "
    "as a catch-all when unsure. Does NOT restart the addon - for that, "
    "see casa_restart_supervised. Restricted to the configurator role.",
    {"scope": str, "role": str, "include_env": bool},
)
async def casa_reload(args: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("casa_reload", caller)

    scope = (args.get("scope") or "").strip()
    if not scope:
        return _result({
            "status": "error", "kind": "scope_required",
            "message": (
                "casa_reload requires a 'scope' argument. Valid: "
                "'agent', 'triggers', 'policies', 'plugin_env', "
                "'agents', 'executors', 'config_sync', 'full'. See doctrine/reload.md."
            ),
        })

    role = (args.get("role") or "").strip() or None
    include_env = bool(args.get("include_env", False))

    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    if runtime is None:
        return _result({
            "status": "error", "kind": "not_initialized",
            "message": "CasaRuntime not bound - boot ordering bug",
        })

    from reload import dispatch
    result = await dispatch(
        scope, runtime=runtime, role=role, include_env=include_env,
    )

    # Drain pending-reload guard if engagement-bound.
    eng = engagement_var.get(None)
    if eng is not None and result.get("status") == "ok":
        _ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)

    return _result(result)


async def _post_supervisor_restart() -> dict:
    """Internal helper used by ``_finalize_engagement`` to honor a
    deferred hard-reload after the bus message + engagement-summary retain have
    landed. Returns a result-shaped dict for logging; never raises.
    """
    import aiohttp
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {
            "status": "error",
            "kind": "no_supervisor_token",
            "message": "SUPERVISOR_TOKEN not set - cannot restart addon",
        }
    headers = {"Authorization": f"Bearer {token}"}
    url = "http://supervisor/addons/self/restart"
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.post(url) as resp:
                return {"supervisor_status": resp.status}
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "kind": "supervisor_error",
            "message": str(exc),
        }


@tool(
    "casa_restart_supervised",
    "Full Supervisor-driven addon restart. Use ONLY when changes require "
    "process-restart semantics (s6 service tree changes, addon "
    "options.json mutations). For routine config edits, use "
    "casa_reload(scope=...) instead. Restricted to the configurator role.",
    {},
)
async def casa_restart_supervised(_: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("casa_restart_supervised", caller)

    eng = engagement_var.get(None)
    if eng is not None:
        # H-1 carry-forward: defer until _finalize_engagement.
        _ENGAGEMENTS_PENDING_RELOAD.discard(eng.id)
        _ENGAGEMENTS_DEFERRED_HARD_RELOAD.add(eng.id)
        return _result({
            "supervisor_status": 200,
            "deferred": True,
            "message": (
                "Supervisor restart deferred until engagement finalizes. "
                "Continue with emit_completion."
            ),
        })

    # Out-of-engagement (operator-driven /invoke etc): POST inline.
    return _result(await _post_supervisor_restart())


# ---------------------------------------------------------------------------
# engage_executor — Plan 3 real impl (configurator + future Tier 3 types)
# ---------------------------------------------------------------------------

# P32 (v0.37.10): duplicate-task guard. Refuses a new engage_executor
# spawn whose ``task=`` overlaps with the most-recent engagement in the
# same channel/chat_id within ``_DUPLICATE_TASK_MAX_AGE_S`` seconds at a
# word-level Jaccard >= ``_DUPLICATE_TASK_JACCARD_THRESHOLD``. Guards
# against the cumulative-context bleed pattern observed in
# ``docs/bug-review-2026-05-14-exploration6.md::O-6``: Ellen's
# back-to-back tool calls re-emitting a prior turn's task as a stale
# second engage_executor argument.
_DUPLICATE_TASK_JACCARD_THRESHOLD = 0.5
_DUPLICATE_TASK_MAX_AGE_S = 60.0
_TASK_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _task_tokens(text: str) -> set[str]:
    return set(_TASK_TOKEN_RE.findall((text or "").lower()))


def _jaccard_task_similarity(a: str, b: str) -> float:
    """Word-level Jaccard similarity on lowercased alphanumeric tokens.

    Returns 0.0 for empty inputs. Used by the P32 duplicate-task guard
    at the ``engage_executor`` MCP call site.
    """
    ta, tb = _task_tokens(a), _task_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


async def _fetch_executor_archive(
    *, task: str, origin_channel: str, token_budget: int,
) -> str:
    """Read prior-engagement "lessons" as a SEMANTIC recall against the shared
    ``casa`` bank, keyed on the current ``task`` and filtered to the originating
    engagement's read-clearance (design §3, plan 3). Returns the digest under a
    recognizable header, or "" when memory is unavailable / the recall is empty.
    Best-effort: ``delegated_recall`` swallows its own errors."""
    import agent as agent_mod
    sem = getattr(agent_mod, "active_semantic_memory", None)
    if sem is None:
        return ""
    digest = await delegated_recall(
        sem, query=task, origin_channel=origin_channel, max_tokens=token_budget,
    )
    return f"## Prior engagements (lessons learned)\n{digest}" if digest else ""


@tool(
    "engage_executor",
    "Start a Tier 3 Executor engagement (configurator, ha-developer, etc.). "
    "Returns engagement_id; result arrives later as a NOTIFICATION.",
    {"executor_type": str, "task": str, "context": str},
)
async def engage_executor(args: dict) -> dict:
    import agent as agent_mod
    # AR-2: snapshot at entry — this handler awaits extensively (topic
    # creation, engagement-registry create, driver dispatch) and reads
    # `origin` well after those awaits; a pooled client's holder rewrite
    # by a later turn must not leak into this in-flight engagement.
    origin = _snapshot_origin()
    if not origin:
        return _result({
            "status": "error", "kind": "no_origin",
            "message": "engage_executor called outside a turn",
        })

    executor_type = args.get("executor_type", "")
    task_text = args.get("task", "") or ""
    context_text = args.get("context", "") or ""

    if _executor_registry is None or not _executor_registry.list_types():
        return _result({
            "status": "error", "kind": "no_executor_types",
            "message": (
                "No Tier 3 Executor types registered. "
                "Ship Plan 3/4/5 or enable an executor in its definition.yaml."
            ),
        })

    defn = _executor_registry.get(executor_type)
    if defn is None:
        return _result({
            "status": "error", "kind": "unknown_executor_type",
            "message": (
                f"No enabled executor type named {executor_type!r}. "
                f"Available: {_executor_registry.list_types()}"
            ),
        })

    # §3.5/§3.8/§3.9: resolve this executor's plugin assignment ONCE — the
    # result feeds the launch gate, the recorded binding, AND the in-casa
    # options (one resolve, one binding). Gate BEFORE topic creation so a
    # blocked launch never leaves a dangling topic.
    def _resolve_and_gate():
        """Returns (plugin_resolution, plugin_artifacts, error_result|None)."""
        res = plugin_registry.resolve_for(f"executor:{executor_type}")
        if not res.registry_valid:
            return res, (), _result({
                "status": "error", "kind": "plugin_registry_invalid",
                "message": ("plugin registry is invalid — executor launches are "
                            "blocked until it is repaired "
                            "(see /data/plugin-health.json)"),
            })
        if res.issues:
            detail = [(i.name, i.reason_code) for i in res.issues]
            return res, (), _result({
                "status": "error", "kind": "plugin_unavailable",
                "message": (f"required plugin(s) unavailable for "
                            f"{executor_type!r}: {detail}"),
            })
        # Sol round-3 B4: also gate on CONFIGURED readiness — a resolvable plugin
        # that is not ready (authorization_missing / unresolved secret / missing
        # system requirement / malformed .mcp.json) must NOT launch with
        # --plugin-dir. Check each assigned plugin's executor-target row through
        # the same verification path.
        target = f"executor:{executor_type}"
        not_ready = []
        for rp in res.plugins:
            v = _tool_verify_plugin_state(plugin_name=rp.name)
            row = next((r for r in v.get("targets", [])
                        if r.get("target") == target), None)
            if row is not None and not row.get("ready"):
                not_ready.append((rp.name, (row.get("reasons") or ["not_ready"])[0]))
            elif row is None and v.get("ready") is not True:
                not_ready.append((rp.name, (v.get("reasons") or ["not_ready"])[0]))
        if not_ready:
            return res, (), _result({
                "status": "error", "kind": "plugin_not_ready",
                "message": (f"plugin(s) not ready for {executor_type!r}: "
                            f"{not_ready} (see /data/plugin-health.json)"),
            })
        arts = tuple(
            {"name": rp.name, "artifact_id": rp.artifact_id, "path": rp.path}
            for rp in res.plugins)
        return res, arts, None

    # Sol #6: capture the snapshot generation at resolve so a concurrent
    # plugin_update during the topic-creation await can be detected before the
    # engagement record pins its artifacts (below, just before create()).
    _resolve_generation = plugin_registry.snapshot_generation()
    plugin_resolution, plugin_artifacts, _gate_err = _resolve_and_gate()
    if _gate_err is not None:
        return _gate_err

    if _channel_manager is None:
        return _result({
            "status": "error", "kind": "no_channel_manager",
            "message": "channel manager missing",
        })
    channel = _channel_manager.get(origin.get("channel", "telegram"))
    # E-F (v0.30.0): if supergroup IS configured but
    # engagement_permission_ok is still False, the boot-time setup may
    # have lost a race with a transient first-boot setWebhook NetworkError.
    # The setup is now wired into _rebuild's tail (self-healing on every
    # reconnect), but in the rare window where the user spawns an
    # engagement before any rebuild has completed, attempt one in-line
    # retry before giving up. Idempotent; cheap on success.
    if (channel is not None
            and getattr(channel, "engagement_supergroup_id", 0)
            and not getattr(channel, "engagement_permission_ok", False)):
        try:
            await channel.setup_engagement_features()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "engage_executor: in-line setup_engagement_features "
                "retry failed: %s", exc,
            )
    if (channel is None
            or not getattr(channel, "engagement_supergroup_id", 0)
            or not getattr(channel, "engagement_permission_ok", False)):
        return _engagement_unavailable_result(origin)  # R-2 (v0.69.7)

    # P32 (v0.37.10): refuse duplicate-task spawns. Compare against the
    # most-recent engagement for the same channel/chat_id within the
    # last _DUPLICATE_TASK_MAX_AGE_S seconds; if word-level Jaccard
    # overlap >= _DUPLICATE_TASK_JACCARD_THRESHOLD, return an error
    # envelope. Guards against the cumulative-context bleed pattern
    # observed in bug-review-2026-05-14-exploration6.md::O-6 (back-to-back
    # Ellen turns re-emitting the prior turn's task). isinstance check
    # falls through gracefully when the registry is a MagicMock in unit
    # tests; production callers always pass a real EngagementRegistry.
    if _engagement_registry is not None and hasattr(
        _engagement_registry, "recent_for_origin",
    ):
        try:
            prior = _engagement_registry.recent_for_origin(
                channel=origin.get("channel", "telegram"),
                chat_id=str(origin.get("chat_id", "")),
                max_age_s=_DUPLICATE_TASK_MAX_AGE_S,
            )
        except Exception as exc:  # noqa: BLE001 — defensive in mock-driven tests
            logger.debug("recent_for_origin lookup skipped: %s", exc)
            prior = None
        if isinstance(prior, EngagementRecord):
            sim = _jaccard_task_similarity(prior.task, task_text)
            if sim >= _DUPLICATE_TASK_JACCARD_THRESHOLD:
                age_s = int(time.time() - prior.started_at)
                return _result({
                    "status": "error", "kind": "duplicate_task",
                    "message": (
                        f"engage_executor refused: task overlaps with "
                        f"engagement {prior.id[:8]} "
                        f"({prior.role_or_type}, started {age_s}s ago) "
                        f"at Jaccard {sim:.2f} >= "
                        f"{_DUPLICATE_TASK_JACCARD_THRESHOLD}. "
                        f"You may be re-emitting a prior turn's task. "
                        f"If you mean a new task, narrow the task= text. "
                        f"If you mean to retry, /cancel {prior.id[:8]} first."
                    ),
                })

    # E-12 (v0.37.0) + v0.37.1 D-1: U3 state-encoded topic title.
    # ``<state-emoji> <concise task>`` per spec §6.3 — the role icon
    # is delivered via the bubble (icon_custom_emoji_id from
    # channels.topic_icons.icon_id_for_role), not the title text.
    from channels.state_emoji import (
        STATE_EMOJI, compose_topic_title, concise_task,
    )
    first_line = (task_text or "").splitlines()[0]
    short_task = concise_task(first_line) or "engagement"
    topic_name = compose_topic_title(
        state="active", short_task=short_task,
    )
    try:
        topic_id = await channel.open_engagement_topic(
            name=topic_name,
            role=executor_type,
        )
    except Exception as exc:  # noqa: BLE001
        return _result({
            "status": "error", "kind": "topic_create_failed",
            "message": str(exc),
        })

    # Sol #6 TOCTOU fence: if a concurrent plugin_update/remove changed the
    # snapshot during the topic-creation await, re-resolve so the engagement
    # record pins the CURRENT artifacts (not ones superseded mid-launch). An
    # update AFTER create() is the by-design informational "previous artifact"
    # case; this only closes the resolve→create window.
    if plugin_registry.snapshot_generation() != _resolve_generation:
        plugin_resolution, plugin_artifacts, _gate_err = _resolve_and_gate()
        if _gate_err is not None:
            await _abort_engagement_topic(channel, "engage-abort", topic_id)
            return _gate_err

    # Computed BEFORE create() so it can be persisted onto the record's
    # origin — the claude_code driver reads it (and context_text) back out
    # of engagement.origin when provisioning the workspace CLAUDE.md.
    world_state = _build_world_state_summary()

    rec = await _engagement_registry.create(
        kind="executor", role_or_type=executor_type, driver=defn.driver,
        task=task_text,
        origin={**origin, "context": context_text, "world_state_summary": world_state},
        topic_id=topic_id,
        tools_allowed=tuple(defn.tools_allowed or ()),
        permission_mode=getattr(defn, "permission_mode", "acceptEdits"),
        plugin_artifacts=plugin_artifacts,          # §3.8 recorded binding
    )

    # Persist the initial state emoji so Task 23 ``update_topic_state`` knows
    # whether it needs to edit the title (no-op when state didn't change).
    try:
        await _engagement_registry.set_channel_state(
            rec.id, current_state_emoji=STATE_EMOJI["active"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_channel_state(active) failed: %s", exc)

    # Read + interpolate prompt template (needed by both driver paths —
    # in_casa: options.system_prompt; claude_code: CLAUDE.md body).
    prompt_template = ""
    try:
        with open(defn.prompt_template_path, "r", encoding="utf-8") as fh:
            prompt_template = fh.read()
    except OSError as exc:
        await _engagement_registry.mark_error(
            rec.id, kind="prompt_template_missing", message=str(exc),
        )
        await _abort_engagement_topic(channel, rec.id, topic_id)
        return _result({
            "status": "error", "kind": "prompt_template_missing",
            "message": str(exc),
        })

    # Semantic-recall memory injection (design §3, plan 3): when the executor
    # opts in (defn.memory.enabled=True, off by default), fetch prior-engagement
    # lessons from the shared `casa` bank at the origin channel's read-clearance.
    executor_memory_block = ""
    if defn.memory.enabled:
        executor_memory_block = await _fetch_executor_archive(
            task=task_text,
            origin_channel=origin.get("channel", "telegram"),
            token_budget=defn.memory.token_budget,
        )

    prompt = (
        prompt_template
        .replace("{task}", task_text)
        .replace("{context}", context_text or "(none)")
        .replace("{world_state_summary}", world_state)
        .replace("{executor_memory}", executor_memory_block)
    )

    # Driver dispatch — in_casa uses ClaudeAgentOptions + system_prompt;
    # claude_code uses the ExecutorDefinition + workspace-CLAUDE.md.
    if defn.driver == "claude_code":
        driver = getattr(agent_mod, "active_claude_code_driver", None)
        if driver is None:
            await _engagement_registry.mark_error(
                rec.id, kind="no_driver",
                message="claude_code driver not initialized",
            )
            await _abort_engagement_topic(channel, rec.id, topic_id)
            return _result({
                "status": "error", "kind": "no_driver",
                "message": "claude_code driver not initialized",
            })
        try:
            await driver.start(rec, prompt=prompt, options=defn)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "claude_code driver.start failed for %s", rec.id[:8],
            )
            await _engagement_registry.mark_error(
                rec.id, kind="driver_start_failed", message=str(exc),
            )
            await _abort_engagement_topic(channel, rec.id, topic_id)
            return _result({
                "status": "error", "kind": "driver_start_failed",
                "message": str(exc),
            })
    else:
        # Off-loop: _build_executor_options reads hooks.yaml. §3.9: feed the
        # SAME resolution gated + recorded above (one resolve, one binding).
        options = await asyncio.to_thread(
            _build_executor_options, defn, executor_type=executor_type,
            resolution=plugin_resolution)
        injected = list(options.allowed_tools or [])
        for t in ("mcp__casa-framework__query_engager",
                  "mcp__casa-framework__emit_completion"):
            if t not in injected:
                injected.append(t)
        options.allowed_tools = injected
        options.system_prompt = prompt

        driver = getattr(agent_mod, "active_engagement_driver", None)
        if driver is None:
            await _engagement_registry.mark_error(
                rec.id, kind="no_driver",
                message="engagement driver not initialized",
            )
            await _abort_engagement_topic(channel, rec.id, topic_id)
            return _result({
                "status": "error", "kind": "no_driver",
                "message": "engagement driver not initialized",
            })
        try:
            await driver.start(rec, prompt=prompt, options=options)
        except Exception as exc:  # noqa: BLE001
            await _engagement_registry.mark_error(
                rec.id, kind="driver_start_failed", message=str(exc),
            )
            await _abort_engagement_topic(channel, rec.id, topic_id)
            return _result({
                "status": "error", "kind": "driver_start_failed",
                "message": str(exc),
            })

    return _result({
        "status": "pending",
        "engagement_id": rec.id,
        "executor_type": executor_type,
        "topic_id": topic_id,
    })


def _engagement_supergroup_chat_id(channel: Any | None) -> int | None:
    """Best-effort chat-id resolution for topic-ledger appends [AR-2].

    Prefer the live telegram channel's configured supergroup id; fall back
    to the boot env the channel would have been built from (casa_core), so
    an append still records a chat_id when telegram is momentarily
    unwired. None when neither is available — the ledger keeps such
    entries but never auto-deletes them.
    """
    chat_id = getattr(channel, "engagement_supergroup_id", None)
    if chat_id:
        return chat_id
    try:
        return int(
            os.environ.get("TELEGRAM_ENGAGEMENT_SUPERGROUP_ID", "0") or 0,
        ) or None
    except (TypeError, ValueError):
        return None


async def _abort_engagement_topic(
    channel: Any, engagement_id: str, topic_id: int | None,
) -> None:
    """Best-effort: flip a just-created topic to 'failed' and close it when
    an engagement dies before its driver started. Never raises.

    Do NOT route these failures through _finalize_engagement — it would
    double-notify Ellen over the bus (the tool already returns the error
    envelope synchronously), overwrite the specific error kind with
    'emit_completion_error', and run memory-retention side effects.
    """
    if topic_id is None:
        return
    # Topic-retention ledger (2026-07-10 design): an aborted engagement's
    # topic is today's most orphan-prone — record it for the retention
    # sweep even when the channel is gone (gate only on topic_id, like the
    # finalize funnel). Own try/except: this function never raises.
    try:
        import topic_ledger
        await topic_ledger.append(
            engagement_id=engagement_id,
            chat_id=_engagement_supergroup_chat_id(channel),
            topic_id=topic_id,
            outcome="error",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "abort topic %s: topic ledger append failed: %s", topic_id, exc,
        )
    if channel is None:
        return
    if hasattr(channel, "update_topic_state"):
        try:
            await channel.update_topic_state(
                engagement_id=engagement_id, new_state="failed",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "abort topic %s: update_topic_state failed: %s", topic_id, exc,
            )
    try:
        await channel.close_topic(thread_id=topic_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("abort topic %s: close_topic failed: %s", topic_id, exc)


# ---------------------------------------------------------------------------
# _finalize_engagement — shared funnel for completion + cancel
# ---------------------------------------------------------------------------


async def _finalize_engagement(
    engagement: EngagementRecord,
    *,
    outcome: str,                       # "completed" | "cancelled" | "error"
    text: str,
    artifacts: list[str],
    next_steps: list[dict],
    driver: Any | None,
    stale_before: float | None = None,
) -> bool:
    """End an engagement: update registry, close topic, NOTIFY Ellen,
    retain a tier-classified engagement summary on the shared ``casa`` bank.

    Never raises on channel/memory side-effects — logs warnings and continues
    so the registry always reaches a terminal state.

    ``stale_before`` (reap only): the terminal transition wins ONLY if the
    record's ``last_user_turn_ts`` is still older than this cutoff — a record
    revived by a user turn since the reap snapshot is left live.

    Returns ``True`` iff this call won the terminal transition and ran the
    finalize side-effects; ``False`` if the record was already terminal or
    the ``stale_before`` guard lost (so the reap doesn't count it).
    """
    now = time.time()

    # 1. Registry transition — atomic and authoritative. Only the first
    #    caller to flip the record terminal runs the finalize side effects
    #    below (L75/L24: guards against a concurrent /cancel racing this
    #    call across a real suspension point, e.g. the G-2 forced-reload
    #    await, which the naive check-then-act in emit_completion cannot).
    if _engagement_registry is not None:
        won = await _engagement_registry.try_transition_terminal(
            engagement.id, outcome,
            completed_at=now if outcome == "completed" else None,
            error_kind="emit_completion_error", error_message=text,
            stale_before=stale_before,
        )
        if not won:
            logger.info(
                "Engagement %s not finalized — already terminal or revived "
                "since snapshot (outcome=%s)",
                engagement.id[:8], outcome,
            )
            return False

    # [AR-4] Topic-retention ledger (2026-07-10 design): record the topic
    # for the retention sweep the moment the record flips terminal — both
    # drivers, all outcomes, regardless of whether close_topic below
    # succeeds. Gated ONLY on topic_id, NOT on channel-manager presence:
    # telegram may be momentarily unwired and the append must still land.
    # Own try/except: a ledger failure must never abort this funnel — the
    # idempotency guard above makes a partial finalize unretryable.
    if engagement.topic_id is not None:
        try:
            import topic_ledger
            ledger_ch = (_channel_manager.get("telegram")
                         if _channel_manager is not None else None)
            await topic_ledger.append(
                engagement_id=engagement.id,
                chat_id=_engagement_supergroup_chat_id(ledger_ch),
                topic_id=engagement.topic_id,
                outcome=outcome,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: topic ledger append failed: %s",
                engagement.id[:8], exc,
            )

    # L68/L17: drop per-engagement observer bookkeeping now that the
    # engagement is terminal — keeps _interjection_counts/_silenced from
    # growing unbounded over the process lifetime.
    try:
        import agent as agent_mod
        _obs = getattr(agent_mod, "active_observer", None)
        if _obs is not None:
            _obs.forget(engagement.id)
    except Exception:  # noqa: BLE001
        pass

    # 2. Post completion message into the topic (if any), flip U3 state, close.
    if engagement.topic_id is not None and _channel_manager is not None:
        tch = _channel_manager.get(engagement.origin.get("channel", "telegram"))
        if tch is not None:
            try:
                await tch.send_to_topic(
                    engagement.topic_id,
                    f"Engagement {outcome}. Summary:\n{text}" if text else f"Engagement {outcome}.",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "finalize engagement %s: send_to_topic failed: %s",
                    engagement.id[:8], exc,
                )
            # E-12 (v0.37.0) Task 23: U3 terminal state — flip the topic title
            # to <state-emoji>·<role-emoji> <task> before closing so the
            # closed-topic sidebar carries the outcome at a glance.
            terminal_state = {
                "completed": "completed",
                "cancelled": "cancelled",
                "error": "failed",
                "failed": "failed",
            }.get(outcome)
            if terminal_state is not None and hasattr(tch, "update_topic_state"):
                try:
                    await tch.update_topic_state(
                        engagement_id=engagement.id, new_state=terminal_state,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "finalize engagement %s: U3 state update failed: %s",
                        engagement.id[:8], exc,
                    )
            try:
                await tch.close_topic(thread_id=engagement.topic_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "finalize engagement %s: close_topic failed: %s",
                    engagement.id[:8], exc,
                )

    # 3. Tear down driver client
    if driver is not None:
        try:
            await driver.cancel(engagement)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: driver.cancel failed: %s",
                engagement.id[:8], exc,
            )

    # Drop the permission-verdict queue for this engagement (leak guard).
    # Lazy import matches this function's existing style and avoids cycles.
    try:
        from channels.channel_handlers import PERMISSION_QUEUES
        PERMISSION_QUEUES.pop(engagement.id, None)
    except Exception:  # noqa: BLE001
        pass

    # 4. NOTIFY Ellen (via existing DelegationComplete-shaped pathway)
    if _bus is not None:
        target_role = engagement.origin.get("role") or "assistant"
        complete = DelegationComplete(
            delegation_id=engagement.id,
            agent=engagement.role_or_type,
            status="ok" if outcome == "completed" else "error",
            text=text,
            kind="" if outcome == "completed" else outcome,
            message=text,
            origin=dict(engagement.origin),
            elapsed_s=now - engagement.started_at,
        )
        try:
            await _bus.notify(BusMessage(
                type=MessageType.NOTIFICATION,
                source=engagement.role_or_type,
                target=target_role,
                content=complete,
                channel=engagement.origin.get("channel", ""),
                context={
                    "cid": engagement.origin.get("cid", "-"),
                    "chat_id": engagement.origin.get("chat_id", ""),
                    "engagement_id": engagement.id,
                    "next_steps": next_steps,
                },
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: bus.notify failed: %s",
                engagement.id[:8], exc,
            )

    # 5. Retain a structured engagement summary on the shared `casa` bank,
    #    tier-classified and gated by the origin channel's write-trust (voice →
    #    nothing) — design §3, plan 3. The post-back NOTIFICATION above is the
    #    durable record for the engager; this is the structured one-shot the
    #    maintainer chose to keep.
    # L33: retain_delegated internally runs an LLM tier-classification per item
    # (a claude-CLI subprocess spawn + round trip) — off the turn's critical
    # path per tier_classifier's own doctrine. Run both retains as background
    # tasks (mirroring _run_delegated_agent) so emit_completion / cancel return
    # promptly; the deferred-reload path below drains them first (H-1).
    retain_tasks: list[asyncio.Task] = []
    import agent as agent_mod
    sem = getattr(agent_mod, "active_semantic_memory", None)
    if sem is not None:
        summary = json.dumps({
            "kind": "engagement_summary",
            "engagement_id": engagement.id,
            "specialist_or_type": engagement.role_or_type,
            "task": engagement.task,
            "status": outcome,
            "started_at": engagement.started_at,
            "completed_at": now,
            "duration_s": now - engagement.started_at,
            "text": text,
            "artifacts": artifacts,
            "next_steps": next_steps,
        })
        bg = asyncio.create_task(retain_delegated(
            sem, origin_channel=str(engagement.origin.get("channel", "")),
            doc_prefix=f"engagement:{engagement.id}:summary",
            turns=[("assistant", summary)],
        ))
        _specialist_bg_tasks.add(bg)
        bg.add_done_callback(_specialist_bg_tasks.discard)
        retain_tasks.append(bg)

    # Plan 4a.1 §8.4: update .casa-meta.json with terminal status + retention_until.
    if engagement.driver == "claude_code":
        try:
            from drivers.workspace import load_casa_meta, write_casa_meta
            ws = os.path.join(_ENGAGEMENTS_ROOT, engagement.id)
            if os.path.isdir(ws):
                meta = load_casa_meta(ws) or {}
                finished_iso = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now),
                )
                retention_iso = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(now + _WORKSPACE_RETENTION_DAYS * 24 * 3600),
                )
                final_status = ("COMPLETED" if outcome == "completed"
                                else "CANCELLED" if outcome == "cancelled"
                                else "ERROR")
                write_casa_meta(
                    workspace_path=ws,
                    engagement_id=engagement.id,
                    executor_type=engagement.role_or_type,
                    status=final_status,
                    created_at=meta.get("created_at") or finished_iso,
                    finished_at=finished_iso,
                    retention_until=retention_iso,
                    # §3.8: the immutable binding survives the terminal rewrite.
                    plugin_artifacts=meta.get("plugin_artifacts"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: .casa-meta update failed: %s",
                engagement.id[:8], exc,
            )

    # Per-executor-type structured summary (only kind=executor), retained
    # tier-tagged on the shared bank with a DISTINCT doc_prefix so it does not
    # clobber the engagement_summary item above.
    # `sem` is the active_semantic_memory resolved in step 5 above.
    if engagement.kind == "executor" and sem is not None:
        type_summary = json.dumps({
            "kind": "executor_engagement_summary",
            "engagement_id": engagement.id,
            "executor_type": engagement.role_or_type,
            "started_at": engagement.started_at,
            "finished_at": now,
            "duration_s": now - engagement.started_at,
            "terminal_state": outcome,
            "engager": engagement.origin.get("role") or "assistant",
            "task": engagement.task,
            "last_text": text,
            "artifacts": artifacts,
        })
        bg = asyncio.create_task(retain_delegated(
            sem, origin_channel=str(engagement.origin.get("channel", "")),
            doc_prefix=f"engagement:{engagement.id}:executor_summary",
            turns=[("assistant", type_summary)],
        ))
        _specialist_bg_tasks.add(bg)
        bg.add_done_callback(_specialist_bg_tasks.discard)
        retain_tasks.append(bg)

    # H-1 (v0.34.0): honor deferred hard-reload now that all bus +
    # retain writes have landed. Only on outcome=completed — per
    # ``completion.md`` doctrine line 61, a cancelled engagement does
    # NOT need a reload (artifact is operator-pending). On
    # outcome=error the engagement bailed; the reload decision is the
    # operator's, not the platform's. Drain the marker on every path
    # to prevent stale state from haunting an idempotent re-call or
    # a follow-up engagement reusing the (very short) id slice.
    deferred_pending = engagement.id in _ENGAGEMENTS_DEFERRED_HARD_RELOAD
    _ENGAGEMENTS_DEFERRED_HARD_RELOAD.discard(engagement.id)
    if outcome == "completed" and deferred_pending:
        # H-1: the Supervisor container-kill must be sequenced AFTER the retain
        # writes have landed. Since L33 moved the retains to background tasks,
        # drain them here (only on this rare deferred-reload path) so the
        # invariant "all retain writes have landed" still holds before restart.
        if retain_tasks:
            await asyncio.gather(*retain_tasks, return_exceptions=True)
        result = await _post_supervisor_restart()
        if result.get("status") == "error":
            logger.warning(
                "finalize engagement %s: deferred Supervisor "
                "restart failed: %s",
                engagement.id[:8], result.get("message"),
            )
        else:
            logger.info(
                "finalize engagement %s: deferred Supervisor restart "
                "POSTed (supervisor_status=%s); container kill arrives "
                "asynchronously, the bus message is already on disk.",
                engagement.id[:8], result.get("supervisor_status"),
            )

    # G-4 (v0.33.0): surface the cause when outcome=error so operators
    # have a starting point for triage. Pre-fix the only log line for
    # this path was an unconditional `logger.info(... outcome=error)`
    # with no reason — exploration2 found a configurator engagement
    # finalized error 24s after system_init with zero log evidence of
    # *why*. Upgrade to WARNING and pull whatever reason fields exist
    # off the registry origin (mark_error stashes kind/message there)
    # plus the text that the emit_completion caller (or the cancel
    # path) passed in.
    if outcome == "error":
        error_kind = engagement.origin.get("error_kind") or "unknown"
        error_message = engagement.origin.get("error_message") or ""
        reason_from_text = (text or "").strip()
        # Prefer registry-stored kind/message (set by mark_error before
        # finalize), then fall back to the text the model emitted.
        composite_reason = (
            error_message or reason_from_text or "no_reason_provided"
        )
        logger.warning(
            "Engagement %s finalized outcome=error kind=%s reason=%s",
            engagement.id[:8], error_kind, composite_reason,
        )
    else:
        logger.info(
            "Engagement %s finalized outcome=%s",
            engagement.id[:8], outcome,
        )
    return True


# ---------------------------------------------------------------------------
# Stale-engagement reap (D-4, v0.69.0)
# ---------------------------------------------------------------------------


_ENGAGEMENT_REAP_DAYS_DEFAULT = 7.0


def _engagement_reap_days() -> float:
    """Reap TTL in days from the ``engagement_reap_days`` add-on option
    (env ``ENGAGEMENT_REAP_DAYS``); 0 disables the reap."""
    raw = os.environ.get("ENGAGEMENT_REAP_DAYS", "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _ENGAGEMENT_REAP_DAYS_DEFAULT


def _resolve_engagement_driver(rec: Any) -> Any | None:
    """Resolve the driver that owns ``rec``'s engagement lifecycle.

    ``claude_code`` executors run as s6-managed subprocesses that only the
    claude_code driver stops/removes; everything else runs in-casa. Shared by
    ``emit_completion``, ``cancel_engagement`` and ``reap_stale_engagements``
    so all three tear down the RIGHT process. (D-4 fix v0.69.6: the reap used
    the in-casa driver for every record, so reaping a claude_code executor
    closed the topic but leaked its subprocess + workspace.)"""
    import agent as agent_mod
    attr = ("active_claude_code_driver" if rec.driver == "claude_code"
            else "active_engagement_driver")
    return getattr(agent_mod, attr, None)


async def reap_stale_engagements(*, ttl_days: float | None = None) -> int:
    """Cancel engagements with no user turn for ``ttl_days`` (D-4, v0.69.0).

    Interrupted/abandoned engagements used to linger active/idle forever
    (25-day-stale engagement, 2026-07-10; restart orphan reaped manually,
    2026-07-11). Runs in the daily engagement sweep BEFORE the idle-reminder
    pass so a to-be-reaped record doesn't get a pointless reminder in the
    same run. Goes through ``_finalize_engagement`` — the same funnel as a
    manual cancel — so the topic is closed + ledger-recorded, the RIGHT
    driver stops the process, Ellen is notified, and the summary retain
    lands. ``stale_before=cutoff`` makes the staleness check part of the
    locked terminal transition, so a record revived by a user turn between
    the snapshot below and the transition is NOT cancelled. Returns the
    number actually reaped.
    """
    if _engagement_registry is None:
        return 0
    if ttl_days is None:
        ttl_days = _engagement_reap_days()
    if ttl_days <= 0:
        return 0
    now = time.time()
    cutoff = now - ttl_days * 86400
    reaped = 0
    for rec in list(_engagement_registry.active_and_idle()):
        if rec.last_user_turn_ts >= cutoff:
            continue
        idle_days = int((now - rec.last_user_turn_ts) // 86400)
        logger.info(
            "reaping stale engagement %s (%s/%s, idle %dd > ttl %gd)",
            rec.id[:8], rec.kind, rec.role_or_type, idle_days, ttl_days,
        )
        try:
            won = await _finalize_engagement(
                rec, outcome="cancelled",
                text=(
                    f"Auto-closed after {idle_days} days with no activity "
                    f"(reap TTL {ttl_days:g}d). Start a new engagement to "
                    "continue this work."
                ),
                artifacts=[], next_steps=[],
                driver=_resolve_engagement_driver(rec),
                stale_before=cutoff,
            )
            if won:
                reaped += 1
        except Exception:  # noqa: BLE001 — one bad record must not stop the sweep
            logger.warning("reap of engagement %s failed", rec.id[:8], exc_info=True)
    return reaped


# ---------------------------------------------------------------------------
# emit_completion — called by the engaged agent
# ---------------------------------------------------------------------------


# B-3 (v0.69.3): the doctrine's status vocabulary (completion.md:31), each
# mapped to its TRUE registry outcome. "error" kept as a legacy alias — the
# tool historically treated any non-ok status as error, so agents may pass it.
_COMPLETION_STATUS_TO_OUTCOME = {
    "ok": "completed",
    "partial": "completed",   # objectives partly met — text carries the marker
    "failed": "error",
    "error": "error",
    "cancelled": "cancelled",
}
_COMPLETION_TEXT_MAX = 8000


@tool(
    "emit_completion",
    "Mark this engagement complete. Ellen receives the summary. Must be called "
    "from inside an active engagement. status: 'ok' | 'partial' | 'failed' | "
    "'cancelled'.",
    {"text": str, "artifacts": list, "next_steps": list, "status": str},
)
async def emit_completion(args: dict) -> dict:
    engagement = engagement_var.get(None)
    if engagement is None:
        return _result({
            "status": "error",
            "kind": "not_in_engagement",
            "message": "emit_completion called outside an engagement",
        })

    # Bug 9 (v0.14.6): idempotency. Re-emitting completion (e.g. SDK
    # retry, hook misfire) used to re-run _finalize_engagement, which
    # double-closes the topic, double-NOTIFYs Ellen, and double-retains
    # the engagement summary on the shared `casa` bank. Re-read the live registry
    # state so we catch transitions that happened on another in-flight
    # turn since this engagement_var snapshot was taken.
    if _engagement_registry is not None:
        live = _engagement_registry.get(engagement.id)
        if live is not None and live.status in (
            "completed", "cancelled", "error",
        ):
            return _result({
                "status": "acknowledged",
                "kind": "already_terminal",
                "message": (
                    f"engagement is already {live.status!r}; "
                    "emit_completion is a no-op."
                ),
            })

    # B-3 (v0.69.3): validate BEFORE any side effect. The old mapping sent
    # EVERY status other than exactly "ok" — including the doctrine's own
    # "partial"/"cancelled", or a model writing "success" — into a terminal
    # outcome=error kind=emit_completion_error, failing fully-successful
    # engagements (2026-07-12 00:14Z incident). A malformed call now comes
    # back as a TOOL error the agent can correct; the engagement stays live.
    status_in = args.get("status", "ok") or "ok"
    if not isinstance(status_in, str) or status_in not in _COMPLETION_STATUS_TO_OUTCOME:
        return _result({
            "status": "error", "kind": "invalid_status",
            "message": (
                f"status={status_in!r} is not a valid completion status; use "
                "'ok' | 'partial' | 'failed' | 'cancelled' (completion.md). "
                "The engagement is still active — call emit_completion again "
                "with a valid status."
            ),
        })
    text = args.get("text", "") or ""
    if not isinstance(text, str):
        return _result({
            "status": "error", "kind": "invalid_args",
            "message": ("text must be a string (got "
                        f"{type(text).__name__}). The engagement is still "
                        "active — call emit_completion again."),
        })
    artifacts = args.get("artifacts") or []
    if isinstance(artifacts, str):
        artifacts = [artifacts]  # a bare SHA is obviously one artifact
    if not isinstance(artifacts, list):
        return _result({
            "status": "error", "kind": "invalid_args",
            "message": ("artifacts must be a list of strings (got "
                        f"{type(artifacts).__name__}). The engagement is "
                        "still active — call emit_completion again."),
        })
    next_steps = args.get("next_steps") or []
    if not isinstance(next_steps, list):
        return _result({
            "status": "error", "kind": "invalid_args",
            "message": ("next_steps must be a list (got "
                        f"{type(next_steps).__name__}). The engagement is "
                        "still active — call emit_completion again."),
        })
    if len(text) > _COMPLETION_TEXT_MAX:
        logger.warning(
            "emit_completion text truncated (%d > %d chars) for engagement %s",
            len(text), _COMPLETION_TEXT_MAX, engagement.id[:8],
        )
        text = text[:_COMPLETION_TEXT_MAX] + " … [truncated]"
    if status_in == "partial":
        text = f"[partial] {text}" if text else "[partial]"
    outcome = _COMPLETION_STATUS_TO_OUTCOME[status_in]

    # Driver is discovered via the agent singleton accessible through the
    # agent module (plan-1 pattern).
    driver = None
    try:
        import agent as agent_mod  # noqa: F401
        if engagement.driver == "claude_code":
            driver = getattr(agent_mod, "active_claude_code_driver", None)
        else:
            driver = getattr(agent_mod, "active_engagement_driver", None)
    except Exception:
        pass

    # G-2 hotfix (v0.33.1): defensive reload guard. If this engagement
    # committed a real change via config_git_commit but never invoked
    # casa_reload / casa_reload_triggers, force-call casa_reload now.
    # The doctrine-only fix in v0.33.0 didn't change model behavior
    # (verify cid `a9313680` 2026-05-01 11:39:57Z); this guard makes
    # post-commit activation a platform invariant rather than a
    # model-compliance contract. Force-call BEFORE _finalize_engagement
    # so the bus message lands after the addon has been told to
    # restart, mirroring the doctrine's own commit-reload-emit order.
    if outcome == "completed" and engagement.id in _ENGAGEMENTS_PENDING_RELOAD:
        logger.warning(
            "Engagement %s emit_completion called with outstanding "
            "reload obligation — config_git_commit landed but no "
            "casa_reload(_triggers) was invoked. Force-calling "
            "casa_reload to honor the post-commit activation contract "
            "(G-2 v0.33.1 defensive guard).",
            engagement.id[:8],
        )
        try:
            # casa_reload is wrapped by @tool — call the underlying
            # handler so we don't pay the SDK envelope-decoding round
            # trip from inside Casa's own code path.
            forced = await casa_reload.handler({})
            logger.info(
                "Engagement %s forced casa_reload result: %s",
                engagement.id[:8],
                json.loads(forced["content"][0]["text"])
                if isinstance(forced, dict)
                and isinstance(forced.get("content"), list)
                and forced["content"]
                else forced,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Engagement %s forced casa_reload raised: %s; "
                "the artifact may remain INERT until manual reload",
                engagement.id[:8], exc,
            )
        finally:
            _ENGAGEMENTS_PENDING_RELOAD.discard(engagement.id)

    await _finalize_engagement(
        engagement,
        outcome=outcome,
        text=text,
        artifacts=artifacts,
        next_steps=next_steps,
        driver=driver,
    )
    # Drain on terminal paths (e.g., outcome=error or
    # already-terminal short-circuit above) — the engagement is gone.
    _ENGAGEMENTS_PENDING_RELOAD.discard(engagement.id)
    return _result({"status": "acknowledged"})


# ---------------------------------------------------------------------------
# query_engager — retrieval + bounded synthesis
# ---------------------------------------------------------------------------


_QUERY_ENGAGER_SYSTEM = (
    "You answer factually using ONLY the provided context. If the context "
    "does not answer the question, reply with exactly: UNKNOWN"
)


async def _synthesize_answer(
    question: str, context: str, max_tokens: int,
) -> str:
    """Run a constrained Anthropic pass via the SDK. Returns the synthesized
    answer, or the literal string 'UNKNOWN' if the context is insufficient.

    Uses SECONDARY_AGENT_MODEL (env-resolved). No tools. No streaming — the
    caller needs a single string.
    """
    import os
    model = os.environ.get("SECONDARY_AGENT_MODEL", "haiku")
    # The pinned claude-agent-sdk's ClaudeAgentOptions has no
    # max_tokens/max_output_tokens field; cap output via the documented
    # Claude Code CLI env knob instead (env merges over the inherited
    # environment for this one CLI subprocess only).
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=_QUERY_ENGAGER_SYSTEM,
        max_turns=1,
        mcp_servers={},
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max(1, max_tokens))},
    )
    prompt = (
        f"Context:\n{context}\n\nQuestion: {question}\n\n"
        f"Answer concisely, in at most about {max_tokens} tokens."
    )
    out = ""
    eng = engagement_var.get(None)
    eng_id = eng.id[:8] if eng is not None else None
    async with ClaudeSDKClient(
        sdk_logging.with_stderr_callback(options, engagement_id=eng_id),
    ) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in getattr(msg, "content", []):
                    if isinstance(b, TextBlock):
                        out += b.text
    out = out.strip()
    # Belt-and-braces hard stop in case the CLI/model still overshoots.
    from tokens import estimate_tokens
    if estimate_tokens(out) > max_tokens:
        out = out[: max_tokens * 4].rstrip()
    return out


@tool(
    "query_engager",
    "Ask the engaging agent a question. Returns synthesized answer from the "
    "engager's clearance-filtered memory, or status=unknown. Callable only from "
    "inside an active engagement.",
    {"question": str, "max_tokens": int},
)
async def query_engager(args: dict) -> dict:
    engagement = engagement_var.get(None)
    if engagement is None:
        return _result({"status": "error", "kind": "not_in_engagement",
                        "message": "query_engager called outside an engagement"})
    question = args.get("question", "") or ""
    max_tokens = max(1, min(int(args.get("max_tokens") or 500), 4000))

    # Retrieve engager-side context: a semantic recall against the shared `casa`
    # bank at the engagement origin's read-clearance (design §3, plan 3).
    import agent as agent_mod
    sem = getattr(agent_mod, "active_semantic_memory", None)
    context = ""
    if sem is not None:
        context = await delegated_recall(
            sem, query=question,
            origin_channel=str(engagement.origin.get("channel", "")),
            max_tokens=2000,
        )

    # Publish bus event so observer can see the query
    if _bus is not None:
        try:
            await _bus.notify(BusMessage(
                type=MessageType.NOTIFICATION,
                source=engagement.role_or_type, target="observer",
                content={
                    "event": "query_engager",
                    "engagement_id": engagement.id,
                    "question": question,
                    "status": "pending",
                },
                context={"engagement_id": engagement.id},
            ))
        except Exception:
            pass

    if not context:
        return _result({"status": "unknown", "text": ""})
    answer = await _synthesize_answer(question, context, max_tokens)
    if answer.strip().upper().startswith("UNKNOWN"):
        return _result({"status": "unknown", "text": ""})
    return _result({"status": "ok", "text": answer})


@tool(
    "cancel_engagement",
    "Cancel an in-flight engagement by id. Closes the topic and NOTIFIES the engager.",
    {"engagement_id": str},
)
async def cancel_engagement(args: dict) -> dict:
    engagement_id = args.get("engagement_id", "") or ""
    if _engagement_registry is None:
        return _result({"status": "error", "kind": "not_initialized",
                        "message": "engagement registry not initialized"})
    rec = _engagement_registry.get(engagement_id)
    if rec is None:
        return _result({"status": "error", "kind": "unknown_engagement",
                        "message": f"no engagement named {engagement_id!r}"})
    if rec.status in ("completed", "cancelled", "error"):
        # L75/L24: a late cancel against an engagement that already
        # finalized (e.g. it raced emit_completion and lost) gets a
        # truthful reply instead of a silent no-op / duplicate finalize.
        return _result({"status": "acknowledged", "kind": "already_terminal",
                        "message": f"engagement is already {rec.status!r}"})

    driver = None
    try:
        import agent as agent_mod  # noqa: F401
        if rec.driver == "claude_code":
            driver = getattr(agent_mod, "active_claude_code_driver", None)
        else:
            driver = getattr(agent_mod, "active_engagement_driver", None)
    except Exception:
        pass

    await _finalize_engagement(
        rec, outcome="cancelled", text="Engagement cancelled.",
        artifacts=[], next_steps=[], driver=driver,
    )
    return _result({"status": "ok", "engagement_id": engagement_id})


# ---------------------------------------------------------------------------
# casa_reload_triggers - back-compat shim for Plan 3 soft-reload (now via dispatch)
# ---------------------------------------------------------------------------


@tool(
    "casa_reload_triggers",
    "Re-register triggers for one agent in-process (no addon restart). "
    "Use when ONLY <role>/triggers.yaml changed. For other config "
    "edits, use casa_reload(scope=...). Restricted to the configurator role.",
    {"role": str},
)
async def casa_reload_triggers(args: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("casa_reload_triggers", caller)

    role = args.get("role")
    if not role:
        return _result({
            "status": "error", "kind": "role_required",
            "message": "casa_reload_triggers requires 'role'",
        })

    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    if runtime is None:
        return _result({
            "status": "error", "kind": "not_initialized",
            "message": "CasaRuntime not bound - boot ordering bug",
        })

    from reload import dispatch
    result = await dispatch("triggers", runtime=runtime, role=role)
    if result.get("status") == "ok":
        result.setdefault("role", role)
        # Back-compat: emit registered=[trigger_names] from runtime.role_configs / specialists
        try:
            cfg = runtime.role_configs.get(role)
            if cfg is None:
                cfg = runtime.specialist_registry.all_configs().get(role)
            if cfg is not None and getattr(cfg, "triggers", None):
                result["registered"] = [t.name for t in cfg.triggers]
        except Exception:  # noqa: BLE001 — best-effort surfacing
            pass
    return _result(result)


# ---------------------------------------------------------------------------
# Plan 4a.1: workspace inspection tools
# ---------------------------------------------------------------------------


def _scan_engagement_workspaces(root: str, status_filter: str | None) -> list[dict]:
    """Blocking scan of /data/engagements — must run via asyncio.to_thread.

    L27: computes a du-style recursive size per workspace (os.walk + per-file
    os.stat). claude_code workspaces can hold cloned repos / node_modules with
    tens of thousands of files, so this runs off the shared event loop.
    """
    from drivers.workspace import load_casa_meta
    entries: list[dict] = []
    for ent in sorted(os.scandir(root), key=lambda e: e.name):
        if not ent.is_dir():
            continue
        meta = load_casa_meta(ent.path) or {}
        if status_filter and meta.get("status") != status_filter:
            continue
        size_bytes = 0
        for dirpath, _dirs, files in os.walk(ent.path):
            for f in files:
                try:
                    size_bytes += os.stat(os.path.join(dirpath, f)).st_size
                except OSError:
                    pass
        entries.append({
            "engagement_id": ent.name,
            "executor_type": meta.get("executor_type"),
            "status": meta.get("status"),
            "created_at": meta.get("created_at"),
            "finished_at": meta.get("finished_at"),
            "retention_until": meta.get("retention_until"),
            "size_bytes": size_bytes,
        })
    return entries


@tool(
    "list_engagement_workspaces",
    "List engagement workspaces under /data/engagements with status + size. "
    "Optional status filter. Truncates at 100 entries.",
    {"status": str},
)
async def list_engagement_workspaces(args: dict) -> dict:
    status_filter = (args.get("status") or "").strip() or None
    root = _ENGAGEMENTS_ROOT

    if not os.path.isdir(root):
        return _result({"workspaces": [], "truncated": False, "total": 0})

    entries = await asyncio.to_thread(_scan_engagement_workspaces, root, status_filter)

    total = len(entries)
    truncated = total > 100
    return _result({
        "workspaces": entries[:100],
        "truncated": truncated,
        "total": total,
    })


_LIVE_ENGAGEMENT_STATES = frozenset({"active", "idle"})


@tool(
    "delete_engagement_workspace",
    "Delete /data/engagements/<id>/ and cancel+finalize the engagement if "
    "still active or idle. Requires force=true to act on a live engagement.",
    {"engagement_id": str, "force": bool},
)
async def delete_engagement_workspace(args: dict) -> dict:
    import shutil

    engagement_id = (args.get("engagement_id") or "").strip()
    force = bool(args.get("force", False))

    if not engagement_id:
        return _result({
            "status": "error", "kind": "bad_request",
            "message": "engagement_id is required",
        })
    if _engagement_registry is None:
        return _result({
            "status": "error", "kind": "not_initialized",
            "message": "engagement registry not wired",
        })

    rec = _engagement_registry.get(engagement_id)
    if rec is None:
        return _result({
            "status": "error", "kind": "unknown_engagement",
            "message": f"no engagement named {engagement_id!r}",
        })

    # Bug 12 (v0.14.6): treat ``idle`` the same as ``active``. An idle
    # engagement is the SDK-suspended-after-24h state — its s6 service
    # and workspace are still live and the driver may resume on the
    # next user turn. Pre-fix the guard only checked ``active`` and
    # quietly yanked an idle workspace out from under a still-running
    # service.
    if rec.status in _LIVE_ENGAGEMENT_STATES and not force:
        return _result({
            "status": "error", "kind": "refused",
            "message": (
                f"engagement is {rec.status!r} (still live); "
                "pass force=true to cancel + delete"
            ),
        })

    if rec.status in _LIVE_ENGAGEMENT_STATES and force:
        # Finalize as cancelled before pulling the workspace.
        driver = None
        try:
            import agent as agent_mod
            driver = (getattr(agent_mod, "active_claude_code_driver", None)
                      if rec.driver == "claude_code"
                      else getattr(agent_mod, "active_engagement_driver", None))
        except Exception:
            pass
        await _finalize_engagement(
            rec, outcome="cancelled",
            text="Workspace deletion forced",
            artifacts=[], next_steps=[],
            driver=driver,
        )

    ws = os.path.join(_ENGAGEMENTS_ROOT, engagement_id)
    if os.path.isdir(ws):
        try:
            shutil.rmtree(ws)
        except OSError as exc:
            return _result({
                "status": "error", "kind": "rmtree_failed",
                "message": f"rmtree {ws}: {exc}",
            })
    # v0.64.0: the per-engagement s6-log dir follows the workspace on this
    # caller-managed path too — once the workspace is gone, the retention
    # sweep can never map to the log dir again.
    from drivers.workspace import engagement_log_dir
    log_dir = engagement_log_dir(engagement_id)
    try:
        if os.path.isdir(log_dir):
            shutil.rmtree(log_dir)
    except OSError as exc:
        logger.warning(
            "delete_engagement_workspace: log dir rmtree %s failed: %s",
            log_dir, exc,
        )
    return _result({
        "status": "ok", "engagement_id": engagement_id,
        "workspace_removed": os.path.isdir(ws) is False,
    })


_PEEK_MAX_DEFAULT = 65_536
_PEEK_MAX_HARD = 524_288


@tool(
    "peek_engagement_workspace",
    "Read-only inspection of /data/engagements/<id>/. Empty path returns a "
    "3-deep tree listing; otherwise reads file contents up to max_bytes "
    "(default 64KB, hard cap 512KB). Path-traversal guarded.",
    {"engagement_id": str, "path": str, "max_bytes": int},
)
async def peek_engagement_workspace(args: dict) -> dict:
    from pathlib import Path as _Path

    engagement_id = (args.get("engagement_id") or "").strip()
    if not engagement_id:
        return _result({"status": "error", "kind": "bad_request",
                        "message": "engagement_id is required"})

    # Security (H15): engagement_id must be a bare workspace name. Real ids
    # are uuid4().hex; reject anything containing path separators or dots so
    # it cannot re-root the workspace via '..' (traversal) or '/config'
    # (absolute join). Without this the traversal guard on `path` below is
    # useless — it anchors on the (already re-rooted) ws_root.resolve().
    if not re.fullmatch(r"[A-Za-z0-9_-]+", engagement_id):
        return _result({"status": "error", "kind": "bad_request",
                        "message": f"invalid engagement_id {engagement_id!r}"})

    ws_root = _Path(_ENGAGEMENTS_ROOT) / engagement_id
    # Defense in depth: the resolved workspace must sit DIRECTLY under the
    # engagements root — never above it or re-rooted elsewhere (also blocks
    # symlink tricks and any future id class).
    if ws_root.resolve().parent != _Path(_ENGAGEMENTS_ROOT).resolve():
        return _result({"status": "error", "kind": "unknown_workspace",
                        "message": f"no workspace for {engagement_id!r}"})
    if not ws_root.is_dir():
        return _result({"status": "error", "kind": "unknown_workspace",
                        "message": f"no workspace for {engagement_id!r}"})

    path_arg = (args.get("path") or "").strip()
    if not path_arg:
        tree = _walk_workspace_tree(ws_root, max_depth=3)
        return _result({"status": "ok", "tree": tree})

    full = (ws_root / path_arg).resolve()
    ws_resolved = ws_root.resolve()
    try:
        full.relative_to(ws_resolved)
    except ValueError:
        return _result({"status": "error", "kind": "path_outside_workspace",
                        "message": f"path {path_arg!r} escapes the workspace"})

    if not full.is_file():
        return _result({"status": "error", "kind": "not_a_file",
                        "message": f"{path_arg!r} is not a regular file"})

    max_bytes = int(args.get("max_bytes") or _PEEK_MAX_DEFAULT)
    if max_bytes > _PEEK_MAX_HARD:
        max_bytes = _PEEK_MAX_HARD
    if max_bytes < 1:
        max_bytes = _PEEK_MAX_DEFAULT

    def _read_prefix() -> str:
        # M26: read only the capped byte prefix — never load the whole file
        # into RAM (a multi-GB workspace log would OOM the container). Cap is
        # in BYTES; a multibyte char split at the boundary decodes to a
        # trailing U+FFFD, which is acceptable for a peek tool.
        with open(full, "rb") as fh:
            data = fh.read(max_bytes)
        return data.decode("utf-8", errors="replace")

    contents = await asyncio.to_thread(_read_prefix)
    return _result({"status": "ok", "path": path_arg, "contents": contents})


def _walk_workspace_tree(root, *, max_depth: int) -> list[dict]:
    out: list[dict] = []
    def _walk(d, depth):
        if depth > max_depth:
            return []
        children: list[dict] = []
        try:
            for e in sorted(os.scandir(d), key=lambda e: e.name):
                entry = {"name": e.name,
                         "type": "dir" if e.is_dir() else "file"}
                if e.is_dir() and depth < max_depth:
                    entry["children"] = _walk(e.path, depth + 1)
                children.append(entry)
        except OSError:
            pass
        return children
    out = _walk(str(root), 1)
    return out


_TOPIC_CLEANUP_SCOPES = ("due", "all_terminal")


@tool(
    "cleanup_engagement_topics",
    "Delete finished engagements' Telegram forum topics recorded in the "
    "topic ledger. scope='due' (default) deletes only entries past the "
    "7-day retention window; 'all_terminal' purges every ledger entry "
    "immediately and is configurator-only. Deletion is irreversible — pass "
    "dry_run=true first to preview what would be deleted.",
    {"scope": str, "dry_run": bool},
)
async def cleanup_engagement_topics(args: dict) -> dict:
    """Configurator-owned on-demand topic cleanup [AR-7] — ledger-only.

    Deletes ONLY topics recorded in the terminal-engagement ledger
    (``/data/topic-ledger.json``): never guesses topic ids, never touches
    active/idle engagements (they are not in the ledger). Deletion is
    IRREVERSIBLE — it removes the topic and all its messages for every
    member — so prefer a ``dry_run=true`` pass first and confirm the
    counts before purging for real (configurator doctrine:
    architecture.md "Engagement-topic cleanup"). The result echoes
    ``dry_run`` and lists the affected topics in ``targets``
    (``{engagement_id, topic_id}`` pairs — would-be deletions under
    dry_run, resolved deletions otherwise) alongside the counts.
    Per-entry telegram failures are classified inside the sweep ([AR-5])
    and reported in ``failures``; entries are retained for retry, never
    dropped on an unrecognized error.
    """
    import topic_ledger

    scope = (args.get("scope") or "due").strip()
    if scope not in _TOPIC_CLEANUP_SCOPES:
        return _result({
            "status": "error", "kind": "bad_scope",
            "message": (
                f"scope must be one of {_TOPIC_CLEANUP_SCOPES}, "
                f"got {scope!r}"
            ),
        })
    # v0.69.12: Ellen (assistant) holds a due-ONLY variant (X2 resolved →
    # webhook trust = authenticated, so the AR-7 deferral is cleared). The
    # irreversible `all_terminal` purge (deletes EVERY ledger topic + all its
    # messages immediately, for all members) stays configurator-only; a
    # non-privileged caller requesting it is refused with a nudge to `due`.
    if scope == "all_terminal" and _effective_caller_role() not in _PRIVILEGED_CONFIG_ROLES:
        return _result({
            "status": "error", "kind": "not_authorized",
            "message": (
                "scope='all_terminal' (immediate purge of every engagement "
                "topic) is restricted to the configurator; use scope='due' to "
                "delete only topics past the retention window."
            ),
        })
    dry_run = bool(args.get("dry_run", False))

    channel = (_channel_manager.get("telegram")
               if _channel_manager is not None else None)
    if channel is None or not getattr(channel, "engagement_supergroup_id", None):
        return _result({
            "status": "error", "kind": "telegram_not_configured",
            "message": ("telegram engagement supergroup is not configured "
                        "— there are no topics to clean up"),
        })

    result = await topic_ledger.sweep_topics(
        channel,
        chat_id=channel.engagement_supergroup_id,
        scope=scope,
        dry_run=dry_run,
    )
    return _result({"status": "ok", **result})


# ---------------------------------------------------------------------------
# Plan 4b §7.1: marketplace_* Configurator MCP tools
# ---------------------------------------------------------------------------


# H16: serialize the mutating plugin/marketplace tools once their blocking
# bodies move off the loop via asyncio.to_thread. On the single event loop
# these handlers were previously mutually exclusive for free (they never
# awaited mid-body); the lock preserves that invariant for concurrent
# marketplace-file / manifest writes. Read-only vault helpers don't take it.
_PLUGIN_TOOLS_LOCK = asyncio.Lock()

# ---------------------------------------------------------------------------
# Unified plugin architecture (§3.9/§3.13): registry-mutating tools.
# ---------------------------------------------------------------------------

_PLUGIN_HEALTH_PATH = "/data/plugin-health.json"


def _regenerate_plugin_health(extra_issues: list) -> None:
    """§3.10/R2-4 + Sol #13: rewrite the health report from the CURRENT resolver
    state PLUS the RUNTIME verify state of EVERY registered plugin — not just the
    plugin this mutation touched. Otherwise a successful mutation of plugin B
    would rewrite health from resolver issues + B's (empty) extras and ERASE
    plugin A's still-active runtime failure (reload_required / authorization_
    missing / unresolved secret). extra_issues carries this mutation's own
    freshly-computed rows; they are de-duplicated against the recompute."""
    import plugin_health
    from plugin_registry import PluginIssue, load_registry
    res = plugin_registry.resolve_all()
    seen = {(getattr(e, "name", None), getattr(e, "target", None),
             getattr(e, "reason_code", None)) for e in extra_issues}
    runtime_issues: list = []
    def _add(name, target, reason):
        key = (name, target, reason)
        if key not in seen:
            seen.add(key)
            runtime_issues.append(PluginIssue(
                name=name, target=target, stage="verify", reason_code=reason))

    reg = load_registry()
    if reg.valid:
        for entry in reg.entries:
            name = entry.get("name")
            try:
                verify = _tool_verify_plugin_state(plugin_name=name)
            except Exception:  # noqa: BLE001
                # Sol round-3 H13: a verifier crash is ITSELF a health problem —
                # surface it, never silently drop the plugin from the report.
                _add(name, None, "verify_exception")
                continue
            rows = verify.get("targets") or []
            for row in rows:
                if not row.get("ready"):
                    _add(name, row.get("target"),
                         (row.get("reasons") or ["not_ready"])[0])
            # Sol round-3 H13: a top-level not-ready with NO target rows (e.g. an
            # unassigned plugin with a missing secret / mcp_invalid) would else be
            # erased — surface it against the plugin itself.
            if verify.get("ready") is not True and not rows:
                for reason in (verify.get("reasons") or ["not_ready"]):
                    _add(name, None, reason)
    plugin_health.write_report(
        issues=list(res.issues) + list(extra_issues) + runtime_issues,
        warnings=list(res.warnings),
        path=_PLUGIN_HEALTH_PATH,
    )


async def _notify_plugin_health_if_possible() -> None:
    if _bus is None:
        return
    try:
        import casa_core
        await casa_core.notify_plugin_health(_bus, path=_PLUGIN_HEALTH_PATH)
    except Exception:  # noqa: BLE001 — never fail a mutation on notify
        logger.debug("plugin health notify skipped", exc_info=True)


def _postcondition_holds(verify: dict, targets: list, *, expect: str,
                         name: str | None = None, runtime=None) -> bool:
    """§3.9 mutation postcondition. 'present' (add/update/assign): every in-casa
    target row must be ready (legacy verify with no 'targets' key is not
    blocking — Task 14's rewrite adds the real rows). 'absent' (unassign/
    remove): no RECONSTRUCTED in-casa agent still binds `name` — read the live
    agent's active_plugin_binding directly, independent of verify shape."""
    if expect == "present":
        # Sol #9: a plugin with zero target rows (e.g. fully unassigned, or an
        # update whose new artifact has unresolved secrets) made all([]) == True
        # → false-green. Require the TOP-LEVEL readiness, which folds in the
        # artifact/tools/secrets checks AND every target row's readiness — so an
        # empty-rows verify no longer passes vacuously.
        return verify.get("ready") is True
    # absent
    if runtime is None or name is None:
        return True
    agents = getattr(runtime, "agents", {}) or {}
    for target in targets:
        tier, _, role = target.partition(":")
        if tier in ("resident", "specialist"):
            agent = agents.get(role)
            if agent is not None and name in getattr(
                    agent, "active_plugin_binding", {}):
                return False
    return True


def _issues_from_mutation(name: str, *, reload_errors: list, verify: dict,
                          expect: str, postcondition_ok: bool) -> list:
    """R2-4: translate reload/verify/postcondition failures into structured
    PluginIssues so they persist in the health report."""
    from plugin_registry import PluginIssue
    issues: list = []
    for err in reload_errors:
        issues.append(PluginIssue(
            name=name, target=err.get("target"), stage="reload",
            reason_code="reload_failed"))
    for row in (verify.get("targets") or []):
        if not row.get("ready"):
            reasons = row.get("reasons") or ["not_ready"]
            issues.append(PluginIssue(
                name=name, target=row.get("target"), stage="verify",
                reason_code=reasons[0]))
    if not postcondition_ok and not reload_errors:
        issues.append(PluginIssue(
            name=name, target=None, stage="verify",
            reason_code="postcondition_failed"))
    return issues


async def _reload_and_verify_targets(name: str, targets: list,
                                     *, expect: str) -> dict:
    """§3.9 mutation sequencing — THE ordering that kills the incident. The
    atomic registry write already happened; now: reload the resolver snapshot
    FIRST, reconstruct affected in-casa agents, desired==active verify, then
    regenerate + notify health. Order is load-bearing (stale-snapshot hazard)."""
    await asyncio.to_thread(plugin_registry.reload_snapshot)   # 1. FIRST
    import agent as agent_mod
    import reload as reload_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    reloaded: list = []
    reload_errors: list = []
    for target in targets:
        tier, _, role = target.partition(":")
        if tier in ("resident", "specialist") and runtime is not None:
            res = await reload_mod.dispatch("agent", runtime=runtime, role=role)
            if res.get("status") == "ok":
                reloaded.append(target)
            else:
                reload_errors.append({"target": target, **res})
        # executors: nothing to reconstruct (per-launch resolution).
    # Sol round-3 B2a: reconstruction leaves the new Agent's _plugin_resolution
    # lazy (None until its first turn) — verify would then classify a live,
    # registered agent as "dormant" and green it BEFORE its binding is captured.
    # Force resolution now so verify sees "active" and confirms binding==desired.
    agents = getattr(runtime, "agents", {}) or {}
    for target in reloaded:
        _, _, role = target.partition(":")
        agent = agents.get(role)
        if agent is not None and getattr(agent, "_plugin_resolution", None) is None:
            try:
                await agent._get_plugin_resolution()
            except Exception:  # noqa: BLE001 — never fail the mutation on resolve
                logger.debug("post-reconstruct resolve failed for %s",
                             role, exc_info=True)
    verify = await asyncio.to_thread(_tool_verify_plugin_state, plugin_name=name)
    ok = (not reload_errors
          and _postcondition_holds(verify, targets, expect=expect,
                                   name=name, runtime=runtime))
    mutation_issues = _issues_from_mutation(
        name, reload_errors=reload_errors, verify=verify,
        expect=expect, postcondition_ok=ok)
    await asyncio.to_thread(_regenerate_plugin_health, mutation_issues)
    await _notify_plugin_health_if_possible()
    result = {"ok": ok, "reloaded": reloaded, "reload_errors": reload_errors,
              "verify": verify}
    if not ok:
        result["kind"] = ("reload_failed" if reload_errors
                          else "postcondition_failed")
    return result


def _install_plugin_sysreqs(name: str, manifest: dict) -> dict | None:
    """§3.3: install a plugin's system requirements BEFORE registry activation.
    Returns an error envelope on failure (registry left unchanged), else None."""
    reqs = plugin_store.manifest_sysreqs(manifest)
    if not reqs:
        return None
    try:
        outcomes = install_requirements(
            plugin_name=name, requirements=reqs,
            tools_root=Path("/config/tools"))
    except OrchestrationError as exc:
        return {"ok": False, "kind": "system_requirements_failed",
                "detail": str(exc)}
    for outcome in outcomes:
        add_manifest(outcome.manifest_entry(name))
    return None


def _plugin_add_sync(*, name: str, repo: str, ref: str, subdir: str = "",
                     targets: list) -> dict:
    """Blocking core of plugin_add: publish → sysreqs → activate. Pure-sync;
    returns the exact envelope contract (ok True|False). Registry stays
    byte-identical on any pre-activation failure (FR2)."""
    if not plugin_registry.NAME_RE.match(name or ""):
        return {"ok": False, "kind": "invalid_name", "name": name}
    targets = list(targets or [])
    bad = [t for t in targets if not plugin_registry.TARGET_RE.match(t)]
    if bad or not targets:
        return {"ok": False, "kind": "invalid_target", "invalid": bad}
    data = plugin_registry.load_registry()                     # from DISK
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}
    if any(isinstance(e, dict) and e.get("name") == name
           for e in data.raw.get("plugins", [])):
        return {"ok": False, "kind": "plugin_exists", "name": name}
    try:
        result = plugin_store.publish(name=name, repo=repo, ref=ref,
                                      subdir=subdir)
    except plugin_store.RefNotFound:
        return {"ok": False, "kind": "ref_not_found"}
    except plugin_store.ResolveUnavailable:
        return {"ok": False, "kind": "resolve_unavailable"}
    except plugin_store.StoreError as exc:
        return {"ok": False, "kind": getattr(exc, "reason_code", "store_error")}
    err = _install_plugin_sysreqs(name, result.manifest)       # BEFORE activate
    if err is not None:
        return err
    data.raw.setdefault("plugins", []).append({
        "name": name,
        "source": {"type": "github", "repo": repo, "ref": ref,
                   "revision": result.revision, "subdir": subdir},
        "artifact_id": result.artifact_id, "version": result.version,
        "targets": targets,
    })
    plugin_registry.save_registry(data)
    return {"ok": True, "name": name, "targets": targets,
            "artifact_id": result.artifact_id, "version": result.version,
            "revision": result.revision, "path": result.path}


def _plugin_update_sync(*, name: str, new_ref: str) -> dict:
    """Blocking core of plugin_update: re-publish from new_ref (version DERIVED
    from the fetched manifest, FR5), install new sysreqs BEFORE moving the
    registry pointer, then repoint the entry. Old artifact retained."""
    data = plugin_registry.load_registry()
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}
    entry = next((e for e in data.raw.get("plugins", [])
                  if isinstance(e, dict) and e.get("name") == name), None)
    if entry is None:
        return {"ok": False, "kind": "not_registered", "name": name}
    src = entry.get("source") or {}
    repo, subdir = src.get("repo", ""), src.get("subdir", "")
    try:
        result = plugin_store.publish(name=name, repo=repo, ref=new_ref,
                                      subdir=subdir)
    except plugin_store.RefNotFound:
        return {"ok": False, "kind": "ref_not_found"}
    except plugin_store.ResolveUnavailable:
        return {"ok": False, "kind": "resolve_unavailable"}
    except plugin_store.StoreError as exc:
        return {"ok": False, "kind": getattr(exc, "reason_code", "store_error")}
    err = _install_plugin_sysreqs(name, result.manifest)       # BEFORE repoint
    if err is not None:
        return err
    entry["source"]["ref"] = new_ref
    entry["source"]["revision"] = result.revision
    entry["artifact_id"] = result.artifact_id
    entry["version"] = result.version
    plugin_registry.save_registry(data)
    return {"ok": True, "name": name, "targets": list(entry.get("targets") or []),
            "artifact_id": result.artifact_id, "version": result.version,
            "revision": result.revision, "path": result.path}


def _resolved_observability(name: str) -> dict:
    """granted_tools + required_env_vars for the freshly-activated plugin (best
    effort — reads the just-reloaded snapshot; resolve_all finds it regardless
    of target)."""
    for rp in plugin_registry.resolve_all().plugins:
        if rp.name == name:
            return {"granted_tools": grants_for_resolved(rp),
                    "required_env_vars": required_env_vars_for_resolved(rp)}
    return {"granted_tools": [], "required_env_vars": []}


@tool(
    "plugin_add",
    "Add a plugin to the registry: publish its pinned artifact, install any "
    "system requirements, assign it to targets, then reload + verify. Version "
    "is derived from the plugin manifest (never supplied).",
    # Sol #15: an explicit JSON Schema — the shorthand {key: type} form marks
    # EVERY key required, so a root-plugin call omitting `subdir` (defaulted by
    # the handler) is rejected by the MCP input validator before the handler
    # runs. `subdir` is the only optional field.
    {"type": "object",
     "properties": {
         "name": {"type": "string"},
         "repo": {"type": "string"},
         "ref": {"type": "string"},
         "subdir": {"type": "string"},
         "targets": {"type": "array"}},
     "required": ["name", "repo", "ref", "targets"]},
)
async def plugin_add(args: dict) -> dict:
    async with _PLUGIN_TOOLS_LOCK:
        core = await asyncio.to_thread(
            _plugin_add_sync, name=args["name"], repo=args["repo"],
            ref=args["ref"], subdir=args.get("subdir", ""),
            targets=args.get("targets") or [])
        if core.get("ok") is not True:
            return _result(core)
        seq = await _reload_and_verify_targets(
            core["name"], core["targets"], expect="present")
        core.update(seq)
        core.update(_resolved_observability(core["name"]))
        return _result(core)


@tool(
    "plugin_update",
    "Update a registered plugin to a new ref: re-publish, install new system "
    "requirements, repoint the registry, reload + verify. Version derives from "
    "the fetched manifest.",
    {"name": str, "new_ref": str},
)
async def plugin_update(args: dict) -> dict:
    async with _PLUGIN_TOOLS_LOCK:
        core = await asyncio.to_thread(
            _plugin_update_sync, name=args["name"], new_ref=args["new_ref"])
        if core.get("ok") is not True:
            return _result(core)
        seq = await _reload_and_verify_targets(
            core["name"], core["targets"], expect="present")
        core.update(seq)
        core.update(_resolved_observability(core["name"]))
        return _result(core)


def _find_entry(data, name: str) -> dict | None:
    return next((e for e in data.raw.get("plugins", [])
                 if isinstance(e, dict) and e.get("name") == name), None)


def _plugin_assign_sync(*, name: str, target: str) -> dict:
    if not plugin_registry.TARGET_RE.match(target or ""):
        return {"ok": False, "kind": "invalid_target", "target": target}
    data = plugin_registry.load_registry()
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}
    entry = _find_entry(data, name)
    if entry is None:
        return {"ok": False, "kind": "not_registered", "name": name}
    targets = entry.setdefault("targets", [])
    was_assigned = target in targets
    if not was_assigned:
        targets.append(target)
        plugin_registry.save_registry(data)
    return {"ok": True, "name": name, "target": target,
            "targets": list(targets), "was_assigned": was_assigned}


def _plugin_unassign_sync(*, name: str, target: str) -> dict:
    data = plugin_registry.load_registry()
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}
    entry = _find_entry(data, name)
    if entry is None:
        return {"ok": False, "kind": "not_registered", "name": name}
    targets = entry.get("targets") or []
    was_assigned = target in targets
    if was_assigned:
        entry["targets"] = [t for t in targets if t != target]
        plugin_registry.save_registry(data)
    return {"ok": True, "name": name, "target": target,
            "was_assigned": was_assigned, "targets": entry.get("targets") or []}


def _plugin_remove_sync(*, name: str) -> dict:
    data = plugin_registry.load_registry()
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}
    entry = _find_entry(data, name)
    if entry is None:
        return {"ok": False, "kind": "not_registered", "name": name}
    targets = list(entry.get("targets") or [])
    data.raw["plugins"] = [
        e for e in data.raw.get("plugins", [])
        if not (isinstance(e, dict) and e.get("name") == name)]
    # §3.1 no-resurrection: seeded_defaults is INTENTIONALLY left untouched, so
    # a removed default stays removed across upgrades. Artifact left for GC.
    plugin_registry.save_registry(data)
    return {"ok": True, "name": name, "targets": targets,
            "artifact_retained": True}


def _tool_plugin_list() -> dict:
    data = plugin_registry.load_registry()
    seeded = set(data.raw.get("seeded_defaults") or [])
    plugins = []
    for e in data.raw.get("plugins", []):
        if not isinstance(e, dict):
            continue
        name, aid = e.get("name"), e.get("artifact_id")
        store_dir = plugin_registry.STORE_ROOT / str(name) / str(aid)
        plugins.append({
            "name": name, "version": e.get("version"),
            "revision": (e.get("source") or {}).get("revision"),
            "targets": e.get("targets") or [], "artifact_id": aid,
            "artifact_present": store_dir.is_dir(),
            "seeded_default": name in seeded,
        })
    return {
        "registry_valid": data.valid, "plugins": plugins,
        "issues": [{"name": i.name, "reason_code": i.reason_code}
                   for i in data.entry_issues],
    }


@tool(
    "plugin_assign",
    "Assign a registered plugin to a target (resident:/specialist:/executor:).",
    {"name": str, "target": str},
)
async def plugin_assign(args: dict) -> dict:
    async with _PLUGIN_TOOLS_LOCK:
        core = await asyncio.to_thread(
            _plugin_assign_sync, name=args["name"], target=args["target"])
        if core.get("ok") is not True:
            return _result(core)
        seq = await _reload_and_verify_targets(
            core["name"], [core["target"]], expect="present")
        core.update(seq)
        return _result(core)


@tool(
    "plugin_unassign",
    "Remove a plugin's assignment to one target (the plugin stays registered).",
    {"name": str, "target": str},
)
async def plugin_unassign(args: dict) -> dict:
    async with _PLUGIN_TOOLS_LOCK:
        core = await asyncio.to_thread(
            _plugin_unassign_sync, name=args["name"], target=args["target"])
        if core.get("ok") is not True:
            return _result(core)
        seq = await _reload_and_verify_targets(
            core["name"], [core["target"]], expect="absent")
        core.update(seq)
        return _result(core)


@tool(
    "plugin_remove",
    "Remove a plugin from the registry entirely (artifact retained for GC).",
    {"name": str},
)
async def plugin_remove(args: dict) -> dict:
    async with _PLUGIN_TOOLS_LOCK:
        core = await asyncio.to_thread(_plugin_remove_sync, name=args["name"])
        if core.get("ok") is not True:
            return _result(core)
        seq = await _reload_and_verify_targets(
            core["name"], core["targets"], expect="absent")
        core.update(seq)
        return _result(core)


@tool(
    "plugin_list",
    "List every registered plugin with its version, revision, targets, "
    "artifact presence, and seeded-default status.",
    {},
)
async def plugin_list(args: dict) -> dict:
    return _result(await asyncio.to_thread(_tool_plugin_list))


# ---------------------------------------------------------------------------
# Plan 4b §7.4–7.6: uninstall + verify_plugin_state + vault helper tools
# ---------------------------------------------------------------------------


def _tool_verify_plugin_state(
    *,
    plugin_name: str,
    _tools_bin: Path | None = None,
    _registry_path=None,
    _store_root=None,
) -> dict:
    """Tier-aware verification (§3.9): does the RUNNING state agree with the
    registry's DESIRED state? Constructed residents/specialists must have the
    desired artifact bound (else reload_required); dormant targets report
    configured readiness only; executors also need their plugin MCP namespaces
    authorized; running engagements on a previous artifact are informational.
    Verification can never report active agreement while a running consumer
    executes different code (FR3).
    """
    import agent as agent_mod
    import cc_tool_pattern
    from plugin_env_conf import read_entries
    from plugin_registry import ResolvedPlugin, STORE_ROOT, load_registry
    from system_requirements.manifest import read_manifest

    reg = (load_registry(_registry_path) if _registry_path is not None
           else load_registry())
    if not reg.valid:
        return {"ready": False, "reasons": ["registry_invalid"],
                "targets": []}
    # Sol #8: verify MUST agree with the resolver. An entry that resolve_for()
    # drops (duplicate_name) or skips (entry_invalid) is unavailable to every
    # agent/executor — verify must not green it off the raw list. Select from
    # the VALIDATED entries and surface the resolver's own rejection reason.
    # Sol round-3 M-shadow: PREFER a validated resolved entry. A rejection issue
    # (entry_invalid / duplicate_name) only blocks when NO validated entry of
    # this name survived — otherwise an unrelated same-name invalid entry would
    # shadow the valid one the resolver actually serves.
    entry = next((e for e in reg.entries
                  if isinstance(e, dict) and e.get("name") == plugin_name), None)
    if entry is None:
        bad = next((i for i in reg.entry_issues if i.name == plugin_name), None)
        if bad is not None:
            return {"ready": False, "reasons": [bad.reason_code], "targets": []}
        return {"ready": False, "reasons": ["not_registered"], "targets": []}

    store_root = _store_root if _store_root is not None else STORE_ROOT
    artifact_id = entry.get("artifact_id")
    src = entry.get("source") or {}
    revision = src.get("revision", "")
    path = Path(store_root) / plugin_name / str(artifact_id)
    reasons: list[str] = []
    provenance_warning = revision.startswith("legacy-content:")
    present = path.is_dir()
    checksum_valid = False
    if not present:
        reasons.append("artifact_missing")
    else:
        verdict = plugin_store.artifact_verdict(
            path, name=plugin_name, repo=src.get("repo", ""),
            revision=revision, subdir=src.get("subdir", ""),
            artifact_id=str(artifact_id))
        if verdict is None:
            checksum_valid = True
        else:
            reasons.append(verdict)          # artifact_invalid | corrupt_artifact

    manifest: dict = {}
    try:
        manifest = json.loads((path / ".claude-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    rp = ResolvedPlugin(name=plugin_name, artifact_id=str(artifact_id),
                        path=str(path), version=str(entry.get("version", "")),
                        manifest=manifest)
    # Sol #16: a PRESENT-but-malformed .mcp.json silently degrades grants/secrets
    # to [] (indistinguishable from skill-only), so a broken MCP server would
    # otherwise verify ready. Treat it as a blocking reason.
    if checksum_valid and mcp_json_malformed(rp):
        reasons.append("mcp_invalid")
    granted = grants_for_resolved(rp) if checksum_valid else []

    # Tools (system-requirements — verify_bin presence). Sol #11: check BOTH the
    # INSTALLED manifest rows AND every requirement the ARTIFACT declares, so a
    # plugin declaring a sysreq with no installed row can't pass on all([]).
    data = read_manifest()
    tool_entries = [p for p in data["plugins"] if p["name"] == plugin_name]
    tools_bin = _tools_bin if _tools_bin is not None else Path("/config/tools/bin")
    tools_status = []
    for t in tool_entries:
        vb = t.get("verify_bin", "")
        ready_bin = (tools_bin / vb).is_file()   # follows symlinks (M23)
        tools_status.append({
            "requirement": t["winning_strategy"], "verify_bin": vb,
            "status": "ready" if ready_bin else "missing",
            **({} if ready_bin else {"reason": f"{vb} not in tools/bin"})})
    # Declared-but-not-installed requirements (Sol #11): every requirement the
    # artifact's manifest declares must have a corresponding installed entry.
    installed_bins = {t.get("verify_bin", "") for t in tool_entries}
    for req in (plugin_store.manifest_sysreqs(manifest) if checksum_valid else []):
        vb = req.get("verify_bin", "")
        if vb and vb not in installed_bins:
            tools_status.append({
                "requirement": req.get("type", "declared"), "verify_bin": vb,
                "status": "missing",
                "reason": f"declared requirement {vb!r} has no installed entry"})

    # Secrets from the ARTIFACT's .mcp.json (no version-dir guessing).
    required = set(required_env_vars_for_resolved(rp)) if checksum_valid else set()
    env_conf = read_entries()
    secrets_status = []
    for var in sorted(required):
        if var in env_conf:
            source = "op" if env_conf[var].startswith("op://") else "plain"
            secrets_status.append({"var": var, "source": source,
                                   "status": "resolved"})
        else:
            secrets_status.append({"var": var, "source": "missing",
                                   "status": "unresolved",
                                   "reason": "not in plugin-env.conf"})

    tools_ready = all(t["status"] == "ready" for t in tools_status)
    secrets_ready = all(s["status"] == "resolved" for s in secrets_status)
    configured_ready = (not reasons) and tools_ready and secrets_ready

    runtime = getattr(agent_mod, "active_runtime", None)
    agents = getattr(runtime, "agents", {}) or {}
    exec_reg = getattr(runtime, "executor_registry", None)

    target_rows = []
    stale_targets = []
    for target in entry.get("targets", []):
        tier, _, role = target.partition(":")
        row = {"target": target, "ready": configured_ready, "reasons": []}
        if not configured_ready:
            row["reasons"] = list(reasons) or ["not_ready"]
        if tier in ("resident", "specialist"):
            agent = agents.get(role)
            constructed = (agent is not None
                           and getattr(agent, "_plugin_resolution", None) is not None)
            if constructed:
                row["state"] = "active"
                active_aid = getattr(agent, "active_plugin_binding", {}).get(plugin_name)
                row["active_artifact_id"] = active_aid
                if active_aid != artifact_id:
                    row["ready"] = False
                    row["reasons"] = ["reload_required"]
                    stale_targets.append(target)
            else:
                row["state"] = "dormant"
        elif tier == "executor":
            row["state"] = "dormant"
            defn = exec_reg.get(role) if exec_reg is not None else None
            allowed = list(getattr(defn, "tools_allowed", []) or []) if defn else []
            missing = [g for g in granted
                       if not cc_tool_pattern.matches_any(allowed, g, {})]
            row["authorization"] = {"required": granted, "missing": missing}
            if missing:
                row["ready"] = False
                row["reasons"] = ["authorization_missing"]
        target_rows.append(row)

    # Running executor engagements on a PREVIOUS artifact — informational.
    sessions = []
    if _engagement_registry is not None:
        try:
            running = _engagement_registry.active_and_idle()
        except Exception:  # noqa: BLE001
            running = []
        for rec in running:
            for pa in getattr(rec, "plugin_artifacts", ()) or ():
                if (pa.get("name") == plugin_name
                        and pa.get("artifact_id") != artifact_id):
                    sessions.append({"engagement_id": rec.id,
                                     "artifact_id": pa.get("artifact_id")})

    # Draining resident/specialist turns still on the PREVIOUS artifact —
    # informational (Sol #4): after a reload swaps an agent, its in-flight turn
    # keeps executing the old artifact until aclose drains it (≤ pool drain
    # timeout). verify discloses it rather than silently implying it's gone.
    for d in getattr(runtime, "draining", None) or []:
        aid = (d.get("binding") or {}).get(plugin_name)
        if aid is not None and aid != artifact_id:
            sessions.append({"draining_role": d.get("role"),
                             "artifact_id": aid})

    top_ready = (configured_ready
                 and all(r["ready"] for r in target_rows))
    return {
        "ready": top_ready,
        "reasons": reasons,
        "desired": {"artifact_id": artifact_id,
                    "version": entry.get("version"),
                    "revision": revision, "targets": entry.get("targets", [])},
        "artifact": {"present": present, "checksum_valid": checksum_valid,
                     "provenance_warning": provenance_warning},
        "tools": tools_status,
        "secrets": secrets_status,
        "granted_tools": granted,
        "targets": target_rows,
        "stale_targets": stale_targets,
        "sessions_on_previous_artifact": sessions,
    }


def _tool_verify_plugin_secrets(*, plugin_name: str) -> dict:
    """Back-compat shim (one release only)."""
    state = _tool_verify_plugin_state(plugin_name=plugin_name)
    return {"secrets": state["secrets"]}


def _tool_set_plugin_env_reference(
    *,
    plugin: str,
    var_name: str,
    op_ref_or_value: str,
) -> dict:
    from plugin_env_conf import set_entry as _set_env_entry_local
    _set_env_entry_local(var_name, op_ref_or_value)
    return {"ok": True}


def _tool_list_vault_items(*, query: str = "", vault: str = "") -> dict:
    cmd = ["op", "item", "list", "--format", "json"]
    if vault:
        cmd += ["--vault", vault]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {"error": r.stderr.strip()}
    items = json.loads(r.stdout)
    if query:
        items = [i for i in items if query.lower() in (i.get("title", "")).lower()]
    return {"items": [{"name": i.get("title"), "id": i.get("id"),
                       "category": i.get("category"),
                       "updated_at": i.get("updated_at")} for i in items]}


def _tool_get_item_fields(*, item: str, vault: str = "") -> dict:
    cmd = ["op", "item", "get", item, "--format", "json"]
    if vault:
        cmd += ["--vault", vault]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {"error": r.stderr.strip()}
    data = json.loads(r.stdout)
    return {"fields": [{"label": f.get("label"),
                        "section": (f.get("section") or {}).get("label", ""),
                        "type": f.get("type")}
                       for f in data.get("fields", [])]}


@tool(
    "verify_plugin_state",
    "Check tool readiness, secret resolution, and MCP cache status for a plugin.",
    {"plugin_name": str},
)
async def verify_plugin_state(args: dict) -> dict:
    return _result(_tool_verify_plugin_state(plugin_name=args["plugin_name"]))


@tool(
    "verify_plugin_secrets",
    "Back-compat shim: check secret resolution for a plugin (use verify_plugin_state instead).",
    {"plugin_name": str},
)
async def verify_plugin_secrets(args: dict) -> dict:
    return _result(_tool_verify_plugin_secrets(plugin_name=args["plugin_name"]))


@tool(
    "set_plugin_env_reference",
    "Upsert a VAR=value or VAR=op://... line in plugin-env.conf.",
    {"plugin": str, "var_name": str, "op_ref_or_value": str},
)
async def set_plugin_env_reference(args: dict) -> dict:
    return _result(_tool_set_plugin_env_reference(
        plugin=args["plugin"],
        var_name=args["var_name"],
        op_ref_or_value=args["op_ref_or_value"],
    ))


@tool(
    "list_vault_items",
    "List 1Password vault items, optionally filtered by query string and/or vault name.",
    {"query": str, "vault": str},
)
async def list_vault_items(args: dict) -> dict:
    return _result(await asyncio.to_thread(
        _tool_list_vault_items,
        query=args.get("query", ""),
        vault=args.get("vault", ""),
    ))


@tool(
    "get_item_fields",
    "Get field labels and types for a 1Password item (does not return secret values).",
    {"item": str, "vault": str},
)
async def get_item_fields(args: dict) -> dict:
    return _result(await asyncio.to_thread(
        _tool_get_item_fields,
        item=args["item"],
        vault=args.get("vault", ""),
    ))


# Module-level tool registry — iterated by create_casa_tools() for the SDK
# path and by the MCP HTTP bridge (mcp_bridge._build_tool_dispatch) for
# real `claude` CLI engagements. Adding a tool here exposes it on both
# transports automatically.
CASA_TOOLS: tuple = (
    send_message,
    delegate_to_agent,
    recall_memory,                 # §4.3 — shared-bank semantic recall (tier-clearance filtered)
    get_schedule,
    engage_executor,
    emit_completion,
    cancel_engagement,
    query_engager,
    config_git_commit,
    casa_reload,
    casa_restart_supervised,            # NEW — Task D.2
    casa_reload_triggers,
    list_engagement_workspaces,
    delete_engagement_workspace,
    peek_engagement_workspace,
    cleanup_engagement_topics,     # v0.65.0 [AR-7] — configurator-only grant
    # Unified plugin architecture (§3.13) — registry tools.
    plugin_add,
    plugin_update,
    plugin_assign,
    plugin_unassign,
    plugin_remove,
    plugin_list,
    verify_plugin_state,
    verify_plugin_secrets,
    set_plugin_env_reference,
    list_vault_items,
    get_item_fields,
)


def create_casa_tools() -> dict[str, Any]:
    """Create and return the casa-framework MCP server config."""
    server = create_sdk_mcp_server(
        name="casa-framework",
        tools=list(CASA_TOOLS),
    )
    return server
