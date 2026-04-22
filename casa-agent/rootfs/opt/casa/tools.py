"""In-process MCP tools for the Casa framework."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trigger_registry import TriggerRegistry

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
from specialist_registry import (
    DelegationComplete,
    DelegationRecord,
    SpecialistRegistry,
)

logger = logging.getLogger(__name__)

# Module-level references, initialized via init_tools()
_channel_manager: ChannelManager | None = None
_bus: MessageBus | None = None
_specialist_registry: SpecialistRegistry | None = None
_mcp_registry: McpServerRegistry | None = None
_trigger_registry: "TriggerRegistry | None" = None


def init_tools(
    channel_manager: ChannelManager,
    bus: MessageBus,
    specialist_registry: SpecialistRegistry,
    mcp_registry: McpServerRegistry | None = None,
    trigger_registry: "TriggerRegistry | None" = None,
) -> None:
    """Initialize module-level references used by tool implementations.

    ``mcp_registry`` is required for specialist MCP-tool resolution at
    delegation time. ``trigger_registry`` is required for the
    ``get_schedule`` tool; callers that don't pass it get a degraded
    tool that returns "not initialized" on every call.
    Accepts ``None`` for legacy callers that don't pass
    it (the `_build_specialist_options` code path degrades to empty MCP
    servers — the specialist still runs but with only built-in tools)."""
    global _channel_manager, _bus, _specialist_registry, _mcp_registry, _trigger_registry  # noqa: PLW0603
    _channel_manager = channel_manager
    _bus = bus
    _specialist_registry = specialist_registry
    _mcp_registry = mcp_registry
    _trigger_registry = trigger_registry


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


def create_casa_tools() -> dict[str, Any]:
    """Create and return the casa-framework MCP server config."""
    server = create_sdk_mcp_server(
        name="casa-framework",
        tools=[send_message, delegate_to_specialist, get_schedule],
    )
    return server
