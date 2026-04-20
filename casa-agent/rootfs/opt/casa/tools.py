"""In-process MCP tools for the Casa framework."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

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
from executor_registry import (
    DelegationComplete,
    DelegationRecord,
    ExecutorRegistry,
)
from mcp_registry import McpServerRegistry

logger = logging.getLogger(__name__)

# Module-level references, initialized via init_tools()
_channel_manager: ChannelManager | None = None
_bus: MessageBus | None = None
_executor_registry: ExecutorRegistry | None = None
_mcp_registry: McpServerRegistry | None = None


def init_tools(
    channel_manager: ChannelManager,
    bus: MessageBus,
    executor_registry: ExecutorRegistry,
    mcp_registry: McpServerRegistry | None = None,
) -> None:
    """Initialize module-level references used by tool implementations.

    ``mcp_registry`` is required for executor MCP-tool resolution at
    delegation time. Accepts ``None`` for legacy callers that don't pass
    it (the `_build_executor_options` code path degrades to empty MCP
    servers — the executor still runs but with only built-in tools)."""
    global _channel_manager, _bus, _executor_registry, _mcp_registry  # noqa: PLW0603
    _channel_manager = channel_manager
    _bus = bus
    _executor_registry = executor_registry
    _mcp_registry = mcp_registry


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


def _result(payload: dict) -> dict:
    """Wrap a JSON-serializable payload as the tool's MCP content."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _build_executor_options(cfg) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a Tier 2 executor invocation.

    Executors run stateless: no hooks (resident-scoped), no session
    resume. MCP servers are resolved per the executor's declared
    ``mcp_server_names`` via the shared registry — same pattern as
    :meth:`Agent._process` (agent.py step 4). Degrades to empty-dict
    when the registry is not bound (legacy callers / test harnesses)."""
    if _mcp_registry is not None:
        mcp_servers = _mcp_registry.resolve(cfg.mcp_server_names)
    else:
        mcp_servers = {}
    return ClaudeAgentOptions(
        model=cfg.model,
        system_prompt=cfg.personality,
        allowed_tools=list(cfg.tools.allowed),
        disallowed_tools=list(cfg.tools.disallowed),
        permission_mode=cfg.tools.permission_mode or "acceptEdits",
        max_turns=cfg.tools.max_turns,
        mcp_servers=mcp_servers if mcp_servers else {},
        hooks={},
        cwd=cfg.cwd or None,
        resume=None,
        setting_sources=["project"],
    )


async def _run_executor(cfg, task_text: str, context_text: str) -> str:
    """Run one ephemeral executor turn and return the concatenated text."""
    options = _build_executor_options(cfg)
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
            loop.create_task(_executor_registry.cancel_delegation(record.id))
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
            loop.create_task(_executor_registry.complete_delegation(record.id))
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
            loop.create_task(_executor_registry.fail_delegation(record.id, exc))

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
    "Delegate a task to a specialized executor agent and return its result.",
    {"agent": str, "task": str, "context": str, "mode": str},
)
async def delegate_to_agent(args: dict) -> dict:
    """Invoke a Tier 2 executor via the SDK and return its text.

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

    if _executor_registry is None:
        return _result({
            "status": "error",
            "kind": "not_initialized",
            "message": "executor registry not initialized",
        })

    # Check origin BEFORE agent lookup: the tool must never dispatch
    # without an origin, even if the name is also invalid. Lets
    # callers test the no-origin branch without first seeding a
    # valid executor.
    origin = agent_mod.origin_var.get(None)
    if origin is None:
        return _result({
            "status": "error",
            "kind": "no_origin",
            "message": "delegate_to_agent called outside a turn",
        })

    cfg = _executor_registry.get(agent_name)
    if cfg is None:
        return _result({
            "status": "error",
            "kind": "unknown_agent",
            "message": f"No enabled executor named {agent_name!r}",
        })

    delegation_id = str(uuid.uuid4())
    started_at = time.time()
    record = DelegationRecord(
        id=delegation_id, agent=agent_name, started_at=started_at,
        origin=dict(origin),
    )
    await _executor_registry.register_delegation(record)

    task = asyncio.create_task(_run_executor(cfg, task_text, context_text))

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
        await _executor_registry.cancel_delegation(delegation_id)
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
        await _executor_registry.fail_delegation(delegation_id, exc)
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
    await _executor_registry.complete_delegation(delegation_id)
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


def create_casa_tools() -> dict[str, Any]:
    """Create and return the casa-framework MCP server config."""
    server = create_sdk_mcp_server(
        name="casa-framework",
        tools=[send_message, delegate_to_agent],
    )
    return server
