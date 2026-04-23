"""In-process MCP tools for the Casa framework."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trigger_registry import TriggerRegistry

from executor_registry import ExecutorRegistry

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

# Module-level references, initialized via init_tools()
_channel_manager: ChannelManager | None = None
_bus: MessageBus | None = None
_specialist_registry: SpecialistRegistry | None = None
_mcp_registry: McpServerRegistry | None = None
_trigger_registry: "TriggerRegistry | None" = None
_engagement_registry: EngagementRegistry | None = None
_executor_registry: "ExecutorRegistry | None" = None
engagement_var: ContextVar[EngagementRecord | None] = ContextVar(
    "engagement_var", default=None,
)


def init_tools(
    channel_manager,
    bus,
    specialist_registry,
    mcp_registry=None,
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
    servers — the specialist still runs but with only built-in tools)."""
    global _channel_manager, _bus, _specialist_registry, _mcp_registry, _trigger_registry, _engagement_registry, _executor_registry  # noqa: PLW0603
    _channel_manager = channel_manager
    _bus = bus
    _specialist_registry = specialist_registry
    _mcp_registry = mcp_registry
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
# delegate_to_specialist — Phase 3.1
# ---------------------------------------------------------------------------


# Phase 3.1: sync-mode wait ceiling. 60 s per spec §6.3. Exposed as a
# module-level constant so tests can monkeypatch to drive the degraded
# path without waiting a minute.
_SYNC_WAIT_TIMEOUT_S: float = 60.0


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

    return ClaudeAgentOptions(
        model=cfg.model,
        system_prompt=cfg.system_prompt,
        allowed_tools=list(cfg.tools.allowed),
        disallowed_tools=list(cfg.tools.disallowed),
        permission_mode=cfg.tools.permission_mode or "acceptEdits",
        max_turns=cfg.tools.max_turns,
        mcp_servers=mcp_servers if mcp_servers else {},
        hooks=resolved_hooks,
        cwd=cfg.cwd or None,
        resume=None,
        setting_sources=["project"],
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

    return ClaudeAgentOptions(
        model=defn.model,
        system_prompt="",
        allowed_tools=list(defn.tools_allowed),
        disallowed_tools=list(defn.tools_disallowed),
        permission_mode=defn.permission_mode or "acceptEdits",
        max_turns=200,
        mcp_servers=mcp_servers if mcp_servers else {},
        hooks=resolved_hooks,
        cwd="/addon_configs/casa-agent",
        resume=None,
        setting_sources=["project"],
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


async def _run_specialist(cfg, task_text: str, context_text: str) -> str:
    """Run one ephemeral specialist turn and return the concatenated text."""
    options = _build_specialist_options(cfg)
    prompt = f"{task_text}\n\nContext:\n{context_text}" if context_text else task_text
    text = ""
    async with ClaudeSDKClient(options) as client:
        await client.query(prompt)
        async for sdk_msg in client.receive_response():
            if isinstance(sdk_msg, AssistantMessage):
                for block in getattr(sdk_msg, "content", []):
                    if isinstance(block, TextBlock):
                        text += block.text
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
    "delegate_to_specialist",
    "Delegate a task to a specialist agent and return its result.",
    {"specialist": str, "task": str, "context": str, "mode": str},
)
async def delegate_to_specialist(args: dict) -> dict:
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

    specialist_name = args.get("specialist", "")
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
            "message": "delegate_to_specialist called outside a turn",
        })

    cfg = _specialist_registry.get(specialist_name)
    if cfg is None:
        return _result({
            "status": "error",
            "kind": "unknown_specialist",
            "message": f"No enabled specialist named {specialist_name!r}",
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
        icon = _ICON_FOR_KIND.get(("specialist", specialist_name), "🧵")
        short_task = (task_text or "").splitlines()[0][:80].strip() or "engagement"
        try:
            topic_id = await channel.open_engagement_topic(
                name=f"#[{specialist_name}] {short_task}",
                icon_emoji=icon,
            )
        except Exception as exc:  # noqa: BLE001
            return _result({"status": "error", "kind": "topic_create_failed",
                            "message": str(exc)})
        # Create record
        rec = await _engagement_registry.create(
            kind="specialist", role_or_type=specialist_name, driver="in_casa",
            task=task_text, origin=dict(origin), topic_id=topic_id,
        )
        # Rename the topic to include the short engagement id for disambiguation
        try:
            await channel.bot.edit_forum_topic(
                chat_id=channel.engagement_supergroup_id,
                message_thread_id=topic_id,
                name=f"#[{specialist_name}] {short_task} · {rec.id[:8]}",
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
            "agent": specialist_name,
            "mode": "interactive",
            "topic_id": topic_id,
        })

    delegation_id = str(uuid.uuid4())
    started_at = time.time()
    record = DelegationRecord(
        id=delegation_id, agent=specialist_name, started_at=started_at,
        origin=dict(origin),
    )
    await _specialist_registry.register_delegation(record)

    task = asyncio.create_task(_run_specialist(cfg, task_text, context_text))

    if mode == "async":
        _attach_completion_callback(task, record)
        logger.info(
            "Delegation %s → %s (async mode)",
            delegation_id[:8], specialist_name,
        )
        return _result({
            "status": "pending",
            "delegation_id": delegation_id,
            "agent": specialist_name,
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
            delegation_id[:8], specialist_name,
        )
        return _result({
            "status": "pending",
            "delegation_id": delegation_id,
            "agent": specialist_name,
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
            delegation_id[:8], specialist_name, kind, exc,
        )
        return _result({
            "status": "error",
            "delegation_id": delegation_id,
            "agent": specialist_name,
            "kind": kind,
            "message": str(exc),
            "elapsed_s": elapsed,
        })

    text = finished.result()
    await _specialist_registry.complete_delegation(delegation_id)
    elapsed = time.time() - started_at
    logger.info(
        "Delegation %s → %s ok (%.2fs)",
        delegation_id[:8], specialist_name, elapsed,
    )
    return _result({
        "status": "ok",
        "delegation_id": delegation_id,
        "agent": specialist_name,
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


@tool(
    "config_git_commit",
    "Stage and commit all tracked changes under /addon_configs/casa-agent/. "
    "Returns the commit SHA (empty if nothing changed).",
    {"message": str},
)
async def config_git_commit(args: dict) -> dict:
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
    "terminated. Only call AFTER emit_completion has been sent.",
    {},
)
async def casa_reload(_: dict) -> dict:
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
        artifacts=[], next_steps=[], driver=driver, memory_provider=None,
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


def create_casa_tools() -> dict[str, Any]:
    """Create and return the casa-framework MCP server config."""
    server = create_sdk_mcp_server(
        name="casa-framework",
        tools=[send_message, delegate_to_specialist, get_schedule, engage_executor,
               emit_completion, cancel_engagement, query_engager,
               config_git_commit, casa_reload, casa_reload_triggers],
    )
    return server
