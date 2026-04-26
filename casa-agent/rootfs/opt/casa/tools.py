"""In-process MCP tools for the Casa framework."""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
from marketplace_ops import (
    MarketplaceError,
    add_plugin_entry,
    list_plugin_entries,
    load_user_marketplace,
    remove_plugin_entry,
    update_plugin_entry,
)
from plugin_env_extractor import extract_env_vars
from plugin_env_conf import set_entry as _set_env_entry  # noqa: F401 — available for future use
from system_requirements.orchestrator import install_requirements, OrchestrationError
from system_requirements.manifest import add_plugin_entry as add_manifest
from plugins_binding import build_sdk_plugins

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
from engagement_registry import EngagementRecord, EngagementRegistry
from specialist_registry import (
    DelegationComplete,
    DelegationRecord,
    SpecialistRegistry,
)

logger = logging.getLogger(__name__)

# Icon map for interactive engagement topic naming.
_ICON_FOR_KIND: dict[tuple[str, str], str] = {
    ("specialist", "finance"): "💰",
    # Plan 3+ adds ("executor", "configurator"): "⚙️", etc.
}

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
    to resolving against ``specialist_registry`` alone (back-compat)."""
    global _channel_manager, _bus, _specialist_registry, _mcp_registry, \
        _agent_role_map, _agent_registry, _trigger_registry, \
        _engagement_registry, _executor_registry  # noqa: PLW0603
    _channel_manager = channel_manager
    _bus = bus
    _specialist_registry = specialist_registry
    _mcp_registry = mcp_registry
    _agent_role_map = dict(agent_role_map or {})
    _agent_registry = agent_registry
    _trigger_registry = trigger_registry
    _engagement_registry = engagement_registry
    _executor_registry = executor_registry


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


def _result(payload: dict) -> dict:
    """Wrap a JSON-serializable payload as the tool's MCP content."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _build_specialist_options(cfg) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a Tier 2 specialist invocation.

    Specialists run stateless (no session resume). Hooks are resolved from
    the specialist's own ``cfg.hooks``. MCP servers are resolved via the
    shared registry — same pattern as :meth:`Agent._process` (agent.py
    step 4). Degrades to empty-dict when the registry is not bound
    (legacy callers / test harnesses)."""
    from hooks import resolve_hooks

    if _mcp_registry is not None:
        mcp_servers = _mcp_registry.resolve(cfg.mcp_server_names)
    else:
        mcp_servers = {}

    resolved_hooks = resolve_hooks(cfg.hooks, default_cwd=cfg.cwd)

    sdk_plugins = build_sdk_plugins(
        home="/addon_configs/casa-agent/cc-home",
        shared_cache="/addon_configs/casa-agent/cc-home/.claude/plugins",
        seed="/opt/claude-seed",
    )

    allowed_tools = list(cfg.tools.allowed)
    if "Skill" not in allowed_tools:
        allowed_tools.append("Skill")

    return ClaudeAgentOptions(
        model=cfg.model,
        system_prompt=cfg.system_prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=list(cfg.tools.disallowed),
        permission_mode=cfg.tools.permission_mode or "acceptEdits",
        max_turns=cfg.tools.max_turns,
        mcp_servers=mcp_servers if mcp_servers else {},
        hooks=resolved_hooks,
        cwd=(cfg.cwd
             or f"/addon_configs/casa-agent/agent-home/{getattr(cfg, 'role', 'unknown')}"),
        resume=None,
        setting_sources=["project"],
        plugins=sdk_plugins,
    )


def _build_executor_options(defn) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a Tier 3 Executor invocation.

    Unlike specialists, executors DO have MCP servers and structured hooks
    driven by their definition.yaml + hooks.yaml. Prompt is injected at
    engage_executor time - this helper does not set system_prompt.
    """
    from config import HooksConfig
    from hooks import resolve_hooks
    import yaml

    hooks_cfg = HooksConfig()
    if defn.hooks_path and os.path.isfile(defn.hooks_path):
        with open(defn.hooks_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        hooks_cfg = HooksConfig(pre_tool_use=list(raw.get("pre_tool_use") or []))

    resolved_hooks = resolve_hooks(hooks_cfg, default_cwd="/addon_configs/casa-agent")

    if _mcp_registry is not None:
        mcp_servers = _mcp_registry.resolve(defn.mcp_server_names)
    else:
        mcp_servers = {}

    sdk_plugins = build_sdk_plugins(
        home="/addon_configs/casa-agent/cc-home",
        shared_cache="/addon_configs/casa-agent/cc-home/.claude/plugins",
        seed="/opt/claude-seed",
    )

    allowed_tools = list(defn.tools_allowed)
    if "Skill" not in allowed_tools:
        allowed_tools.append("Skill")

    # Executors (in_casa driver — Configurator, future Tier-3) operate on
    # the addon-config root rather than an agent-home, because their
    # mutation surface spans /addon_configs/casa-agent/ (agents/, marketplace/,
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
        cwd="/addon_configs/casa-agent",
        resume=None,
        setting_sources=["project"],
        plugins=sdk_plugins,
    )


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
    agents_dir = "/addon_configs/casa-agent/agents"
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

    try:
        import agent as agent_mod
        scope_reg = getattr(agent_mod, "active_scope_registry", None)
        scopes = sorted(scope_reg._scopes.keys()) if scope_reg else []
    except Exception:  # noqa: BLE001
        scopes = []
    lines.append(f"Scopes:               {', '.join(scopes) or '(none)'}")

    version = "unknown"
    for candidate in ("/opt/casa/VERSION", "/addon_configs/casa-agent/VERSION"):
        try:
            with open(candidate) as fh:
                version = fh.read().strip()
                break
        except OSError:
            continue
    lines.append(f"Addon version:        {version}")

    return "\n".join(lines)


async def _run_delegated_agent(cfg, task_text: str, context_text: str) -> str:
    """Run one ephemeral delegated turn and return the concatenated text."""
    import agent as agent_mod
    parent = agent_mod.origin_var.get(None) or {}
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
        body = (
            f"{delegation_context}\n\n"
            f"Task: {task_text}\n\n"
            f"Context from {caller_name}:\n{context_text}"
        )
    else:
        body = (
            f"{delegation_context}\n\n"
            f"Task: {task_text}"
        )
    prompt = body

    options = _build_specialist_options(cfg)
    text = ""
    token = agent_mod.origin_var.set(child_origin)
    try:
        async with ClaudeSDKClient(options) as client:
            await client.query(prompt)
            async for sdk_msg in client.receive_response():
                if isinstance(sdk_msg, AssistantMessage):
                    for block in getattr(sdk_msg, "content", []):
                        if isinstance(block, TextBlock):
                            text += block.text
    finally:
        agent_mod.origin_var.reset(token)
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
    origin = agent_mod.origin_var.get(None)
    if origin is None:
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
        if (channel is None
                or not getattr(channel, "engagement_supergroup_id", 0)
                or not getattr(channel, "engagement_permission_ok", False)):
            return _result({
                "status": "error", "kind": "engagement_not_configured",
                "message": ("set telegram_engagement_supergroup_id in addon "
                            "options and verify the bot has can_manage_topics"),
            })
        # Open topic
        icon = _ICON_FOR_KIND.get(("specialist", agent_name), "🧵")
        short_task = (task_text or "").splitlines()[0][:80].strip() or "engagement"
        try:
            topic_id = await channel.open_engagement_topic(
                name=f"#[{agent_name}] {short_task}",
                icon_emoji=icon,
            )
        except Exception as exc:  # noqa: BLE001
            return _result({"status": "error", "kind": "topic_create_failed",
                            "message": str(exc)})
        # Create record
        rec = await _engagement_registry.create(
            kind="specialist", role_or_type=agent_name, driver="in_casa",
            task=task_text, origin=dict(origin), topic_id=topic_id,
        )
        # Rename the topic to include the short engagement id for disambiguation
        try:
            await channel.bot.edit_forum_topic(
                chat_id=channel.engagement_supergroup_id,
                message_thread_id=topic_id,
                name=f"#[{agent_name}] {short_task} · {rec.id[:8]}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("edit_forum_topic rename failed: %s", exc)

        # Build options + start driver
        options = _build_specialist_options(cfg)
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
            return _result({"status": "error", "kind": "no_driver",
                            "message": "engagement driver not initialized"})
        try:
            await driver.start(rec, prompt=prompt, options=options)
        except Exception as exc:  # noqa: BLE001
            await _engagement_registry.mark_error(rec.id, kind="driver_start_failed",
                                                  message=str(exc))
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
    import agent as agent_mod

    if _trigger_registry is None:
        return {"content": [{"type": "text",
                             "text": "Error: trigger registry not initialized"}]}

    origin = agent_mod.origin_var.get(None)
    if origin is None:
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

    Resolves SDK-path turns via ``origin_var`` and engagement-bridge
    turns (claude_code executors) via ``engagement_var.role_or_type``.
    Returns None if neither context is bound — in which case the tool
    must refuse rather than fall back to permissive default.
    """
    try:
        import agent as agent_mod
        origin = agent_mod.origin_var.get(None)
        if origin is not None:
            r = origin.get("role")
            if r:
                return r
    except Exception:  # noqa: BLE001 - defensive against import-time issues
        pass
    eng = engagement_var.get(None)
    if eng is not None:
        return getattr(eng, "role_or_type", None)
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
    "Stage and commit all tracked changes under /addon_configs/casa-agent/. "
    "Returns the commit SHA (empty if nothing changed). "
    "Restricted to the configurator executor role.",
    {"message": str},
)
async def config_git_commit(args: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("config_git_commit", caller)

    message = args.get("message") or "configurator: commit"
    try:
        import config_git
        sha = await asyncio.to_thread(
            config_git.commit_config, "/addon_configs/casa-agent", message,
        )
        return _result({"sha": sha, "message": message})
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
    "Restart the Casa addon via Supervisor. Your own session will be "
    "terminated. Only call AFTER emit_completion has been sent. "
    "Restricted to the configurator executor role.",
    {},
)
async def casa_reload(_: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("casa_reload", caller)

    import aiohttp
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return _result({
            "status": "error",
            "kind": "no_supervisor_token",
            "message": "SUPERVISOR_TOKEN not set - cannot restart addon",
        })
    headers = {"Authorization": f"Bearer {token}"}
    url = "http://supervisor/addons/self/restart"
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.post(url) as resp:
                return _result({"supervisor_status": resp.status})
    except Exception as exc:  # noqa: BLE001
        return _result({
            "status": "error",
            "kind": "supervisor_error",
            "message": str(exc),
        })


# ---------------------------------------------------------------------------
# engage_executor — Plan 3 real impl (configurator + future Tier 3 types)
# ---------------------------------------------------------------------------


@tool(
    "engage_executor",
    "Start a Tier 3 Executor engagement (configurator, ha-developer, etc.). "
    "Returns engagement_id; result arrives later as a NOTIFICATION.",
    {"executor_type": str, "task": str, "context": str},
)
async def engage_executor(args: dict) -> dict:
    import agent as agent_mod
    origin = agent_mod.origin_var.get(None)
    if origin is None:
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

    if _channel_manager is None:
        return _result({
            "status": "error", "kind": "no_channel_manager",
            "message": "channel manager missing",
        })
    channel = _channel_manager.get(origin.get("channel", "telegram"))
    if (channel is None
            or not getattr(channel, "engagement_supergroup_id", 0)
            or not getattr(channel, "engagement_permission_ok", False)):
        return _result({
            "status": "error", "kind": "engagement_not_configured",
            "message": ("set telegram_engagement_supergroup_id in addon "
                        "options and verify the bot has can_manage_topics"),
        })

    short_task = (task_text or "").splitlines()[0][:80].strip() or "engagement"
    try:
        topic_id = await channel.open_engagement_topic(
            name=f"#[{executor_type}] {short_task}",
            icon_emoji="tools",
        )
    except Exception as exc:  # noqa: BLE001
        return _result({
            "status": "error", "kind": "topic_create_failed",
            "message": str(exc),
        })

    rec = await _engagement_registry.create(
        kind="executor", role_or_type=executor_type, driver=defn.driver,
        task=task_text, origin=dict(origin), topic_id=topic_id,
    )

    try:
        await channel.bot.edit_forum_topic(
            chat_id=channel.engagement_supergroup_id,
            message_thread_id=topic_id,
            name=f"#[{executor_type}] {short_task} | {rec.id[:8]}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("edit_forum_topic rename failed: %s", exc)

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
        return _result({
            "status": "error", "kind": "prompt_template_missing",
            "message": str(exc),
        })

    world_state = _build_world_state_summary()
    prompt = (
        prompt_template
        .replace("{task}", task_text)
        .replace("{context}", context_text or "(none)")
        .replace("{world_state_summary}", world_state)
    )

    # Driver dispatch — in_casa uses ClaudeAgentOptions + system_prompt;
    # claude_code uses the ExecutorDefinition + workspace-CLAUDE.md.
    if defn.driver == "claude_code":
        driver = getattr(agent_mod, "active_claude_code_driver", None)
        if driver is None:
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
            return _result({
                "status": "error", "kind": "driver_start_failed",
                "message": str(exc),
            })
    else:
        options = _build_executor_options(defn)
        injected = list(options.allowed_tools or [])
        for t in ("mcp__casa-framework__query_engager",
                  "mcp__casa-framework__emit_completion"):
            if t not in injected:
                injected.append(t)
        options.allowed_tools = injected
        options.system_prompt = prompt

        driver = getattr(agent_mod, "active_engagement_driver", None)
        if driver is None:
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
    memory_provider: Any | None,
) -> None:
    """End an engagement: update registry, close topic, NOTIFY Ellen, write
    Ellen's meta-scope summary.

    Never raises on channel/memory side-effects — logs warnings and continues
    so the registry always reaches a terminal state.
    """
    now = time.time()

    # 1. Registry transition
    if _engagement_registry is not None:
        if outcome == "completed":
            await _engagement_registry.mark_completed(engagement.id, completed_at=now)
        elif outcome == "cancelled":
            await _engagement_registry.mark_cancelled(engagement.id)
        else:  # "error"
            await _engagement_registry.mark_error(
                engagement.id, kind="emit_completion_error", message=text,
            )

    # 2. Post completion message into the topic (if any) and close it
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
            try:
                await tch.close_topic_with_check(thread_id=engagement.topic_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "finalize engagement %s: close_topic_with_check failed: %s",
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

    # 5. Write Ellen's meta-scope summary (best-effort)
    if memory_provider is not None:
        try:
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
            # Use Ellen's meta session. The session_id convention is
            # {channel}:{chat_id}:meta:assistant — mirror plan-1 scope stack.
            channel = engagement.origin.get("channel", "telegram")
            chat_id = str(engagement.origin.get("chat_id", ""))
            await memory_provider.ensure_session(
                session_id=f"{channel}:{chat_id}:meta:assistant",
                agent_role="assistant",
            )
            await memory_provider.add_turn(
                session_id=f"{channel}:{chat_id}:meta:assistant",
                agent_role="assistant",
                user_text="(engagement summary written)",
                assistant_text=summary,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: meta summary write failed: %s",
                engagement.id[:8], exc,
            )

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
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: .casa-meta update failed: %s",
                engagement.id[:8], exc,
            )

    # Plan 4a: per-executor-type Honcho archival (only for kind=executor).
    if (memory_provider is not None
            and engagement.kind == "executor"):
        try:
            channel = engagement.origin.get("channel", "telegram")
            chat_id = str(engagement.origin.get("chat_id", ""))
            type_session = f"{channel}:{chat_id}:executor:{engagement.role_or_type}"
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
            await memory_provider.ensure_session(
                session_id=type_session,
                agent_role=f"executor:{engagement.role_or_type}",
            )
            await memory_provider.add_turn(
                session_id=type_session,
                agent_role=f"executor:{engagement.role_or_type}",
                user_text="(executor engagement summary)",
                assistant_text=type_summary,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: executor-type archival failed: %s",
                engagement.id[:8], exc,
            )

    logger.info(
        "Engagement %s finalized outcome=%s",
        engagement.id[:8], outcome,
    )


# ---------------------------------------------------------------------------
# emit_completion — called by the engaged agent
# ---------------------------------------------------------------------------


@tool(
    "emit_completion",
    "Mark this engagement complete. Ellen receives the summary. Must be called "
    "from inside an active engagement.",
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
    # double-closes the topic, double-NOTIFYs Ellen, and double-writes
    # the meta-scope summary into Honcho. Re-read the live registry
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

    text = args.get("text", "") or ""
    artifacts = list(args.get("artifacts") or [])
    next_steps = list(args.get("next_steps") or [])
    status_in = args.get("status", "ok") or "ok"
    outcome = "completed" if status_in == "ok" else "error"

    # Driver + memory_provider are discovered via the agent singleton
    # accessible through the agent module (plan-1 pattern).
    driver = None
    memory_provider = None
    try:
        import agent as agent_mod  # noqa: F401
        if engagement.driver == "claude_code":
            driver = getattr(agent_mod, "active_claude_code_driver", None)
        else:
            driver = getattr(agent_mod, "active_engagement_driver", None)
        memory_provider = getattr(agent_mod, "active_memory_provider", None)
    except Exception:
        pass

    await _finalize_engagement(
        engagement,
        outcome=outcome,
        text=text,
        artifacts=artifacts,
        next_steps=next_steps,
        driver=driver,
        memory_provider=memory_provider,
    )
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
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=_QUERY_ENGAGER_SYSTEM,
        max_turns=1,
        mcp_servers={},
    )
    prompt = f"Context:\n{context}\n\nQuestion: {question}"
    out = ""
    async with ClaudeSDKClient(options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in getattr(msg, "content", []):
                    if isinstance(b, TextBlock):
                        out += b.text
    return out.strip()


@tool(
    "query_engager",
    "Ask the engaging agent a question. Returns synthesized answer from the "
    "engager's scope-filtered memory, or status=unknown. Callable only from "
    "inside an active engagement.",
    {"question": str, "max_tokens": int},
)
async def query_engager(args: dict) -> dict:
    engagement = engagement_var.get(None)
    if engagement is None:
        return _result({"status": "error", "kind": "not_in_engagement",
                        "message": "query_engager called outside an engagement"})
    question = args.get("question", "") or ""
    max_tokens = int(args.get("max_tokens") or 500)

    # Retrieve engager-side context
    memory_provider = None
    try:
        import agent as agent_mod
        memory_provider = getattr(agent_mod, "active_memory_provider", None)
    except Exception:
        pass
    engager_role = engagement.origin.get("role", "assistant")
    channel = engagement.origin.get("channel", "telegram")
    chat_id = str(engagement.origin.get("chat_id", ""))
    engager_scope = engagement.origin.get("scope", "meta")
    session_id = f"{channel}:{chat_id}:{engager_scope}:{engager_role}"

    context = ""
    if memory_provider is not None:
        try:
            context = await memory_provider.get_context(
                session_id=session_id,
                agent_role=engager_role,
                tokens=2000,
                search_query=question,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "query_engager: get_context failed (%s); returning unknown", exc,
            )
            context = ""

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

    driver = None
    memory_provider = None
    try:
        import agent as agent_mod  # noqa: F401
        if rec.driver == "claude_code":
            driver = getattr(agent_mod, "active_claude_code_driver", None)
        else:
            driver = getattr(agent_mod, "active_engagement_driver", None)
        memory_provider = getattr(agent_mod, "active_memory_provider", None)
    except Exception:
        pass

    await _finalize_engagement(
        rec, outcome="cancelled", text="Engagement cancelled.",
        artifacts=[], next_steps=[], driver=driver,
        memory_provider=memory_provider,
    )
    return _result({"status": "ok", "engagement_id": engagement_id})


# ---------------------------------------------------------------------------
# casa_reload_triggers - Plan 3 (soft reload for triggers.yaml edits only)
# ---------------------------------------------------------------------------


@tool(
    "casa_reload_triggers",
    "Re-register triggers for one agent in-process (no addon restart). "
    "Use when ONLY <role>/triggers.yaml changed. For anything else, use casa_reload.",
    {"role": str},
)
async def casa_reload_triggers(args: dict) -> dict:
    role = args["role"]
    if _trigger_registry is None:
        return _result({
            "status": "error",
            "kind": "not_initialized",
            "message": "trigger registry not wired",
        })

    import agent_loader
    base = "/addon_configs/casa-agent"
    agents_dir = os.path.join(base, "agents")
    agent_dir = None
    # Look in residents (top-level) and specialists/
    for candidate in (os.path.join(agents_dir, role),
                      os.path.join(agents_dir, "specialists", role)):
        if os.path.isdir(candidate):
            agent_dir = candidate
            break
    if agent_dir is None:
        return _result({
            "status": "error",
            "kind": "unknown_role",
            "message": f"no agent directory found for role={role!r}",
        })

    try:
        cfg = await asyncio.to_thread(
            agent_loader.load_agent_from_dir, agent_dir, policies=None,
        )
    except Exception as exc:  # noqa: BLE001
        return _result({
            "status": "error",
            "kind": "load_error",
            "message": str(exc),
        })

    try:
        await asyncio.to_thread(
            _trigger_registry.reregister_for,
            role, list(cfg.triggers), list(cfg.channels),
        )
    except Exception as exc:  # noqa: BLE001
        return _result({
            "status": "error",
            "kind": "reregister_failed",
            "message": str(exc),
        })

    return _result({
        "status": "ok",
        "role": role,
        "registered": [t.name for t in cfg.triggers],
    })


# ---------------------------------------------------------------------------
# Plan 4a.1: workspace inspection tools
# ---------------------------------------------------------------------------


@tool(
    "list_engagement_workspaces",
    "List engagement workspaces under /data/engagements with status + size. "
    "Optional status filter. Truncates at 100 entries.",
    {"status": str},
)
async def list_engagement_workspaces(args: dict) -> dict:
    from drivers.workspace import load_casa_meta

    status_filter = (args.get("status") or "").strip() or None
    root = _ENGAGEMENTS_ROOT

    if not os.path.isdir(root):
        return _result({"workspaces": [], "truncated": False, "total": 0})

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
        memory_provider = None
        try:
            import agent as agent_mod
            driver = (getattr(agent_mod, "active_claude_code_driver", None)
                      if rec.driver == "claude_code"
                      else getattr(agent_mod, "active_engagement_driver", None))
            memory_provider = getattr(agent_mod, "active_memory_provider", None)
        except Exception:
            pass
        await _finalize_engagement(
            rec, outcome="cancelled",
            text="Workspace deletion forced",
            artifacts=[], next_steps=[],
            driver=driver, memory_provider=memory_provider,
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

    ws_root = _Path(_ENGAGEMENTS_ROOT) / engagement_id
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

    contents = full.read_text(errors="replace")[:max_bytes]
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


# ---------------------------------------------------------------------------
# Plan 4b §7.1: marketplace_* Configurator MCP tools
# ---------------------------------------------------------------------------


def _tool_marketplace_add_plugin(
    *,
    plugin_name: str,
    repo_url: str,
    ref: str,
    description: str,
    category: str = "productivity",
    version: str | None = None,
    casa_system_requirements: list[dict] | None = None,
) -> dict:
    # Normalize repo_url → github source shape.
    repo = repo_url.replace("https://github.com/", "").rstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]

    entry = {
        "name": plugin_name,
        "description": description,
        "version": version or ref,
        "source": {"source": "github", "repo": repo, "sha": ref},
        "category": category,
    }
    if casa_system_requirements:
        entry["casa"] = {"systemRequirements": casa_system_requirements}

    try:
        add_plugin_entry(entry)
    except MarketplaceError as exc:
        return {"added": False, "error": str(exc)}

    # Refresh CC's view of the user marketplace.
    subprocess.run(
        ["claude", "plugin", "marketplace", "update", "casa-plugins"],
        capture_output=True, text=True, timeout=30,
    )
    return {"added": True, "entry": entry}


def _tool_marketplace_remove_plugin(*, plugin_name: str) -> dict:
    try:
        remove_plugin_entry(plugin_name)
    except MarketplaceError as exc:
        return {"removed": False, "error": str(exc)}
    subprocess.run(
        ["claude", "plugin", "marketplace", "update", "casa-plugins"],
        capture_output=True, text=True, timeout=30,
    )
    return {"removed": True}


def _tool_marketplace_update_plugin(*, plugin_name: str, new_ref: str) -> dict:
    try:
        update_plugin_entry(plugin_name, new_ref=new_ref)
    except MarketplaceError as exc:
        return {"updated": False, "error": str(exc)}
    subprocess.run(
        ["claude", "plugin", "marketplace", "update", "casa-plugins"],
        capture_output=True, text=True, timeout=30,
    )
    return {"updated": True, "new_ref": new_ref}


def _tool_marketplace_list_plugins() -> dict:
    return {"plugins": list_plugin_entries()}


# ---------------------------------------------------------------------------
# Plan 4b §7.3 / §4.3.3: install_casa_plugin two-stage commit
# ---------------------------------------------------------------------------

_INSTALL_LOCK = "/addon_configs/casa-agent/cc-home/.claude/plugins/.install.lock"
_AGENT_HOME_ROOT = Path("/addon_configs/casa-agent/agent-home")


def _tool_install_casa_plugin(
    *,
    plugin_name: str,
    targets: list[str],
) -> dict:
    # 1. Validate marketplace entry exists.
    data = load_user_marketplace()
    entry = next((p for p in data["plugins"] if p["name"] == plugin_name), None)
    if entry is None:
        return {"ok": False, "error": "plugin_not_in_marketplace"}

    # 2. Refresh CC's view.
    subprocess.run(
        ["claude", "plugin", "marketplace", "update", "casa-plugins"],
        capture_output=True, text=True, timeout=30,
    )

    # 3. Stage 1 — install system requirements (if any).
    reqs = (entry.get("casa") or {}).get("systemRequirements") or []
    outcomes: list = []
    if reqs:
        try:
            outcomes = install_requirements(
                plugin_name=plugin_name,
                requirements=reqs,
                tools_root=Path("/addon_configs/casa-agent/tools"),
            )
        except OrchestrationError as exc:
            return {"ok": False, "error": "system_requirements_failed", "detail": str(exc)}

        # Record manifest BEFORE stage 2 so reconciler can recover on crash.
        for outcome in outcomes:
            add_manifest(outcome.manifest_entry(plugin_name))

    # 4. Stage 2 — claude plugin install in each agent-home.
    installed: list[str] = []
    failed: list[str] = []
    for role in targets:
        agent_home = _AGENT_HOME_ROOT / role
        agent_home.mkdir(parents=True, exist_ok=True)
        cmd = [
            "flock", _INSTALL_LOCK,
            "claude", "plugin", "install",
            f"{plugin_name}@casa-plugins", "--scope", "project",
        ]
        r = subprocess.run(cmd, cwd=agent_home, capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            installed.append(role)
        else:
            failed.append(role)

    if failed:
        # Best-effort rollback of stage 1.
        for outcome in outcomes:
            shutil.rmtree(outcome.install_dir, ignore_errors=True)
        return {
            "ok": False,
            "error": "agent_install_failed",
            "failed": failed,
            "installed": installed,
        }

    # 5. Extract required env vars from cached plugin's .mcp.json.
    cache_root = Path(
        "/addon_configs/casa-agent/cc-home/.claude/plugins/cache/casa-plugins"
    )
    mcp_json = next(
        iter(cache_root.glob(f"{plugin_name}/*/.mcp.json")),
        None,
    )
    env_vars = extract_env_vars(mcp_json) if mcp_json else set()

    return {
        "ok": True,
        "installed_on": installed,
        "required_env_vars": sorted(env_vars),
        "system_requirements_installed": len(outcomes),
    }


@tool(
    "marketplace_add_plugin",
    "Add a plugin entry to the user marketplace.",
    {
        "plugin_name": str,
        "repo_url": str,
        "ref": str,
        "description": str,
        "category": str,
        "version": str,
        "casa_system_requirements": list,
    },
)
async def marketplace_add_plugin(args: dict) -> dict:
    return _result(_tool_marketplace_add_plugin(
        plugin_name=args["plugin_name"],
        repo_url=args["repo_url"],
        ref=args["ref"],
        description=args["description"],
        category=args.get("category", "productivity"),
        version=args.get("version"),
        casa_system_requirements=args.get("casa_system_requirements"),
    ))


@tool(
    "marketplace_remove_plugin",
    "Remove a plugin entry from the user marketplace.",
    {"plugin_name": str},
)
async def marketplace_remove_plugin(args: dict) -> dict:
    return _result(_tool_marketplace_remove_plugin(plugin_name=args["plugin_name"]))


@tool(
    "marketplace_update_plugin",
    "Update a plugin's sha/ref in the user marketplace.",
    {"plugin_name": str, "new_ref": str},
)
async def marketplace_update_plugin(args: dict) -> dict:
    return _result(_tool_marketplace_update_plugin(
        plugin_name=args["plugin_name"],
        new_ref=args["new_ref"],
    ))


@tool(
    "marketplace_list_plugins",
    "List all plugins in the user marketplace.",
    {},
)
async def marketplace_list_plugins(args: dict) -> dict:
    return _result(_tool_marketplace_list_plugins())


@tool(
    "install_casa_plugin",
    "Install a plugin into target agents (two-stage commit: system requirements then per-agent-home plugin install).",
    {
        "plugin_name": str,
        "targets": list,
    },
)
async def install_casa_plugin(args: dict) -> dict:
    return _result(_tool_install_casa_plugin(
        plugin_name=args["plugin_name"],
        targets=args["targets"],
    ))


# ---------------------------------------------------------------------------
# Plan 4b §7.4–7.6: uninstall + verify_plugin_state + vault helper tools
# ---------------------------------------------------------------------------


def _tool_uninstall_casa_plugin(
    *,
    plugin_name: str,
    targets: list[str] | None = None,
) -> dict:
    if targets is None:
        targets = []
        if _AGENT_HOME_ROOT.is_dir():
            for d in _AGENT_HOME_ROOT.iterdir():
                settings = d / ".claude" / "settings.json"
                if not settings.is_file():
                    continue
                data = json.loads(settings.read_text(encoding="utf-8"))
                if any(k.startswith(f"{plugin_name}@") for k in data.get("enabledPlugins", {})):
                    targets.append(d.name)

    uninstalled: list[str] = []
    for role in targets:
        agent_home = _AGENT_HOME_ROOT / role
        cmd = ["claude", "plugin", "uninstall",
               f"{plugin_name}@casa-plugins", "--scope", "project"]
        r = subprocess.run(cmd, cwd=agent_home, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            uninstalled.append(role)
    return {"uninstalled_from": uninstalled}


def _tool_verify_plugin_state(
    *,
    plugin_name: str,
    _tools_bin: Path | None = None,
    _cache_root: Path | None = None,
) -> dict:
    """Check tool readiness, secret resolution, and MCP cache for a plugin.

    The optional ``_tools_bin`` and ``_cache_root`` parameters override the
    production paths for testing.
    """
    from system_requirements.manifest import read_manifest
    from plugin_env_conf import read_entries

    data = read_manifest()
    tool_entries = [p for p in data["plugins"] if p["name"] == plugin_name]

    tools_bin = _tools_bin if _tools_bin is not None else Path("/addon_configs/casa-agent/tools/bin")
    tools_status = []
    for t in tool_entries:
        vb = t.get("verify_bin", "")
        if (tools_bin / vb).is_symlink() or (tools_bin / vb).is_file():
            tools_status.append({"requirement": t["winning_strategy"], "verify_bin": vb,
                                 "status": "ready"})
        else:
            tools_status.append({"requirement": t["winning_strategy"], "verify_bin": vb,
                                 "status": "missing",
                                 "reason": f"{vb} not in tools/bin"})

    cache_root = _cache_root if _cache_root is not None else Path(
        "/addon_configs/casa-agent/cc-home/.claude/plugins/cache/casa-plugins"
    )
    mcp_json = next(iter(cache_root.glob(f"{plugin_name}/*/.mcp.json")), None)
    required = extract_env_vars(mcp_json) if mcp_json else set()
    env_conf = read_entries()
    secrets_status = []
    for var in sorted(required):
        if var in env_conf:
            source = "op" if env_conf[var].startswith("op://") else "plain"
            secrets_status.append({"var": var, "source": source, "status": "resolved"})
        else:
            secrets_status.append({"var": var, "source": "missing",
                                   "status": "unresolved",
                                   "reason": "not in plugin-env.conf"})

    mcp_started = mcp_json is not None
    mcp_errors: list[dict] = []

    ready = (
        all(t["status"] == "ready" for t in tools_status)
        and all(s["status"] == "resolved" for s in secrets_status)
        and mcp_started
    )
    return {
        "tools": tools_status,
        "secrets": secrets_status,
        "mcp_started": mcp_started,
        "mcp_errors": mcp_errors,
        "ready": ready,
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
    "uninstall_casa_plugin",
    "Uninstall a plugin from target agent-homes (or all homes that have it enabled if targets omitted).",
    {"plugin_name": str, "targets": list},
)
async def uninstall_casa_plugin(args: dict) -> dict:
    return _result(_tool_uninstall_casa_plugin(
        plugin_name=args["plugin_name"],
        targets=args.get("targets") or None,
    ))


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
    return _result(_tool_list_vault_items(
        query=args.get("query", ""),
        vault=args.get("vault", ""),
    ))


@tool(
    "get_item_fields",
    "Get field labels and types for a 1Password item (does not return secret values).",
    {"item": str, "vault": str},
)
async def get_item_fields(args: dict) -> dict:
    return _result(_tool_get_item_fields(
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
    get_schedule,
    engage_executor,
    emit_completion,
    cancel_engagement,
    query_engager,
    config_git_commit,
    casa_reload,
    casa_reload_triggers,
    list_engagement_workspaces,
    delete_engagement_workspace,
    peek_engagement_workspace,
    marketplace_add_plugin,
    marketplace_remove_plugin,
    marketplace_update_plugin,
    marketplace_list_plugins,
    install_casa_plugin,
    uninstall_casa_plugin,
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
