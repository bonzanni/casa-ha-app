"""Core Agent class -- orchestrates SDK, memory, sessions, and channels."""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from config import AgentConfig
from executor_registry import DelegationComplete
from hooks import resolve_hooks
from log_cid import cid_var
from mcp_registry import McpServerRegistry
from channel_trust import channel_trust, user_peer_for_channel
from memory import MemoryProvider
from session_registry import SessionRegistry, build_session_key
from retry import retry_sdk_call
from tokens import (
    BudgetTracker,
    estimate_tokens,
    extract_usage,
    format_turn_summary,
)
from error_kinds import ErrorKind, _classify_error, _USER_MESSAGES  # noqa: F401 — re-exported

logger = logging.getLogger(__name__)

# Phase 3.1: delegating-turn origin. Set by Agent._process for the
# duration of a turn so the `delegate_to_agent` tool handler can read
# the channel/chat_id/cid/role/user_text of the outer user turn without
# threading them through function arguments. ContextVar semantics —
# asyncio.create_task snapshots the value, so late-completing executor
# tasks still see the right origin.
origin_var: ContextVar[dict | None] = ContextVar("origin_var", default=None)

# Type alias for the streaming callback
OnTokenCallback = Callable[[str], Awaitable[None]]


class Agent:
    """A Casa agent backed by the Claude Agent SDK."""

    def __init__(
        self,
        config: AgentConfig,
        memory: MemoryProvider,
        session_registry: SessionRegistry,
        mcp_registry: McpServerRegistry,
        channel_manager: ChannelManager,
    ) -> None:
        self.config = config
        self._memory = memory
        self._session_registry = session_registry
        self._mcp_registry = mcp_registry
        self._channel_manager = channel_manager
        self._bg_tasks: set[asyncio.Task] = set()
        # Per-(session_id) over-budget streak tracker (spec 5.2 §5.2).
        # Per-instance so assistant (4000) and butler (800) budgets stay
        # isolated even when the same channel serves both roles.
        self._budget_tracker = BudgetTracker()
        # Resolve hooks once at construction. HooksConfig.pre_tool_use
        # empty → default policy bundle (block_dangerous_bash + path_scope
        # scoped to cfg.cwd).
        self._resolved_hooks = resolve_hooks(
            config.hooks, default_cwd=config.cwd,
        )

    # ------------------------------------------------------------------
    # Public entry point (used as bus handler)
    # ------------------------------------------------------------------

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        """Process an inbound message and return a response BusMessage.

        If the channel supports streaming, tokens are delivered
        incrementally via ``on_token``.  The full response is sent or
        finalized after the SDK completes.
        """
        # Phase 3.1: late-completion delegation NOTIFICATION — synthesize
        # a fresh turn so the delegating resident narrates the result
        # back to the user on the origin channel.
        if (
            msg.type == MessageType.NOTIFICATION
            and isinstance(msg.content, DelegationComplete)
        ):
            msg = self._synthesize_delegation_turn(msg)

        # Obtain a streaming callback from the channel (if available)
        on_token: OnTokenCallback | None = None
        channel = self._channel_manager.get(msg.channel) if msg.channel else None

        if channel is not None and hasattr(channel, "create_on_token"):
            on_token = channel.create_on_token(msg.context)

        error_kind: ErrorKind | None = None
        try:
            text = await self._process(msg, on_token=on_token)
        except Exception as exc:
            error_kind = _classify_error(exc)
            logger.error(
                "Agent '%s' error [%s]: %s",
                self.config.character.name,
                error_kind.value,
                exc,
                exc_info=(error_kind == ErrorKind.UNKNOWN),
            )
            text = _USER_MESSAGES[error_kind]

        # Deliver the response via the channel.
        # For voice (or any channel that supplies emit_error_line), prefer
        # the persona-voice error pipeline on error paths. Otherwise fall
        # through to the existing text-based finalize_stream / send flow.
        if error_kind is not None and channel is not None \
                and hasattr(channel, "emit_error_line"):
            try:
                handled = await channel.emit_error_line(
                    error_kind.value, msg.context, self.config,
                )
            except Exception:
                logger.exception("emit_error_line raised; falling back to text")
                handled = False
            if handled:
                text = ""  # suppress normal text delivery below

        if text and channel is not None:
            if on_token is not None and hasattr(channel, "finalize_stream"):
                await channel.finalize_stream(text, msg.context, on_token)
            else:
                await channel.send(text, msg.context)

        if not text and error_kind is None:
            return None

        return BusMessage(
            type=MessageType.RESPONSE,
            source=self.config.role,
            target=msg.source,
            content=text or "",
            reply_to=msg.id,
            channel=msg.channel,
            context=msg.context,
        )

    def _synthesize_delegation_turn(self, msg: BusMessage) -> BusMessage:
        """Convert a NOTIFICATION+DelegationComplete into a REQUEST turn
        whose content is a synth prompt for the delegating resident."""
        complete = msg.content
        assert isinstance(complete, DelegationComplete)
        short_id = complete.delegation_id[:8]

        origin = complete.origin or {}
        user_text = origin.get("user_text", "")

        if complete.status == "ok":
            body = (
                f"[System notification: your delegation to {complete.agent} "
                f"(id {short_id}) has returned with status=ok]\n\n"
                f"Result text from {complete.agent}:\n{complete.text}\n"
            )
        elif complete.kind == "restart_orphan":
            body = (
                f"[System notification: your delegation to {complete.agent} "
                f"(id {short_id}) was orphaned by a Casa restart]\n\n"
                "I lost track of this delegation during a Casa restart. "
                "Tell the user and offer to retry.\n"
            )
        else:
            body = (
                f"[System notification: your delegation to {complete.agent} "
                f"(id {short_id}) has returned with status=error]\n\n"
                f"Delegation failed ({complete.kind or 'unknown'}): "
                f"{complete.message}\n"
            )
        body += (
            f"\nThe original user question was: {user_text}\n\n"
            "Reply to the user via their original channel. Be concise.\n"
        )

        return BusMessage(
            type=MessageType.REQUEST,
            source=msg.source,
            target=msg.target,
            content=body,
            channel=msg.channel,
            context=dict(msg.context),
        )

    # ------------------------------------------------------------------
    # Internal processing pipeline
    # ------------------------------------------------------------------

    async def _process(
        self,
        msg: BusMessage,
        on_token: OnTokenCallback | None = None,
    ) -> str | None:
        channel_key = build_session_key(
            msg.channel,
            msg.context.get("chat_id"),
        )
        session_id = f"{channel_key}:{self.config.role}"
        user_peer = user_peer_for_channel(msg.channel)
        user_text = str(msg.content)

        origin_token = origin_var.set({
            "role": self.config.role,
            "channel": msg.channel,
            "chat_id": msg.context.get("chat_id", ""),
            "cid": cid_var.get(),
            "user_text": user_text,
        })
        try:
            # 1. Ensure session + peers (idempotent, cheap when warm). ----------
            try:
                await self._memory.ensure_session(
                    session_id=session_id,
                    agent_role=self.config.role,
                    user_peer=user_peer,
                )
            except Exception:
                logger.warning(
                    "Memory ensure_session failed; continuing without memory",
                )

            # 2. Retrieve memory digest + record budget usage (spec 5.2 §5.2).
            memory_context = ""
            try:
                memory_context = await self._memory.get_context(
                    session_id=session_id,
                    agent_role=self.config.role,
                    tokens=self.config.memory.token_budget,
                    search_query=user_text,
                    user_peer=user_peer,
                )
            except Exception:
                logger.warning(
                    "Memory retrieval failed; proceeding without context",
                )
            else:
                # Only on success: a failed memory call gives us nothing to
                # measure. The tracker is no-op when budget <= 0.
                self._budget_tracker.record(
                    session_id,
                    estimate_tokens(memory_context),
                    self.config.memory.token_budget,
                )

            # 3. System prompt = composed-prompt + runtime-injected blocks.
            system_parts = [self.config.system_prompt]
            if memory_context:
                system_parts.append(
                    f"\n<memory_context>\n{memory_context}\n</memory_context>"
                )
            from channel_trust import channel_trust_display
            system_parts.append(
                "\n<channel_context>\n"
                f"channel: {msg.channel}\n"
                f"trust: {channel_trust_display(msg.channel)}\n"
                "</channel_context>"
            )
            system_prompt = "\n".join(system_parts)

            # 4. MCP servers ---------------------------------------------------
            mcp_servers = self._mcp_registry.resolve(self.config.mcp_server_names)

            # 5. Hooks — resolved from hooks.yaml at load time by agent_loader.
            hooks = self._resolved_hooks

            # 6. SDK resume --------------------------------------------------
            existing = self._session_registry.get(channel_key)
            resume_session_id: str | None = None
            if existing:
                resume_session_id = existing.get("sdk_session_id")
                await self._session_registry.touch(channel_key)

            options = ClaudeAgentOptions(
                model=self.config.model,
                system_prompt=system_prompt,
                allowed_tools=self.config.tools.allowed,
                disallowed_tools=self.config.tools.disallowed,
                permission_mode=self.config.tools.permission_mode or "acceptEdits",
                max_turns=self.config.tools.max_turns,
                mcp_servers=mcp_servers if mcp_servers else {},
                hooks=hooks,
                cwd=self.config.cwd or None,
                resume=resume_session_id,
                setting_sources=["project"],
            )

            # 7. Query the SDK — retry transient faults (spec 5.2 §3). --------
            async def _attempt_sdk_turn() -> tuple[str, str | None, dict[str, int]]:
                """Run one end-to-end SDK turn. Each attempt resets the
                streaming accumulator so ``on_token`` delivers cumulative
                text from scratch if an earlier attempt failed mid-turn.
                ``attempt_usage`` resets per attempt so a failed attempt's
                partial usage cannot leak into the turn_done summary
                (spec 5.2 §5.2)."""
                attempt_text = ""
                attempt_sid: str | None = resume_session_id
                attempt_usage: dict[str, int] = {}
                async with ClaudeSDKClient(options) as client:
                    await client.query(user_text)
                    async for sdk_msg in client.receive_response():
                        if isinstance(sdk_msg, SystemMessage):
                            if getattr(sdk_msg, "subtype", None) == "init":
                                data = getattr(sdk_msg, "data", {}) or {}
                                if "session_id" in data:
                                    attempt_sid = data["session_id"]
                        elif isinstance(sdk_msg, ResultMessage):
                            sid = getattr(sdk_msg, "session_id", None)
                            if sid:
                                attempt_sid = sid
                            attempt_usage = extract_usage(sdk_msg)
                        elif isinstance(sdk_msg, AssistantMessage):
                            for block in getattr(sdk_msg, "content", []):
                                if isinstance(block, TextBlock):
                                    attempt_text += block.text
                                    if on_token is not None:
                                        await on_token(attempt_text)
                return attempt_text, attempt_sid, attempt_usage

            try:
                response_text, sdk_session_id, usage = await retry_sdk_call(
                    _attempt_sdk_turn, on_retry=self._log_retry,
                )
            except ProcessError:
                # claude CLI exited non-zero. If we were resuming a prior
                # session, the most common cause (spec 5.8) is a stale
                # sdk_session_id — the local conversation file under
                # ``/root/.claude/`` was wiped (rebuild) while
                # ``/data/sessions.json`` persisted. Clear and retry fresh.
                if resume_session_id is None:
                    raise
                logger.warning(
                    "SDK resume failed (key=%s sid=%s); clearing and retrying fresh",
                    channel_key, resume_session_id,
                )
                await self._session_registry.clear_sdk_session(channel_key)
                resume_session_id = None
                options = replace(options, resume=None)
                response_text, sdk_session_id, usage = await retry_sdk_call(
                    _attempt_sdk_turn, on_retry=self._log_retry,
                )

            # Per-turn telemetry (spec 5.2 §5.2). Microsecond cost — string
            # format + one logger.info — and runs after streaming has
            # already flushed via on_token, so it is not on the voice
            # critical path.
            logger.info(
                format_turn_summary(
                    self.config.role,
                    msg.channel or "-",
                    usage,
                ),
            )

            if sdk_session_id and sdk_session_id != resume_session_id:
                logger.info(
                    "SDK session for '%s': %s",
                    self.config.role,
                    sdk_session_id,
                )

            # 8. Persist — off the critical path. Storage is unconditional. ---
            #    Session+peer topology already scopes visibility (spec §4.3).
            if response_text:
                task = asyncio.create_task(self._add_turn_bg(
                    session_id, self.config.role, user_text, response_text, user_peer,
                ))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)

            # 9. SessionRegistry — only SDK session id now. --------------------
            if sdk_session_id:
                await self._session_registry.register(
                    channel_key=channel_key,
                    agent=self.config.role,
                    sdk_session_id=sdk_session_id,
                )

            return response_text or None
        finally:
            origin_var.reset(origin_token)

    async def _add_turn_bg(
        self,
        session_id: str,
        agent_role: str,
        user_text: str,
        assistant_text: str,
        user_peer: str,
    ) -> None:
        """Persist a turn in the background. Exceptions are caught and
        logged — never surfaced to the user (the response has already
        been delivered). Spec §11."""
        try:
            await self._memory.add_turn(
                session_id=session_id,
                agent_role=agent_role,
                user_text=user_text,
                assistant_text=assistant_text,
                user_peer=user_peer,
            )
        except Exception as exc:
            logger.warning(
                "Memory add_turn failed in background: %s", exc,
            )

    def _log_retry(self, attempt: int, exc: Exception, delay_ms: int) -> None:
        """Emit a single WARNING per retry event (spec 5.2 §3.2)."""
        kind = _classify_error(exc)
        logger.warning(
            "SDK retry: role=%s attempt=%d kind=%s delay_ms=%d exc=%r",
            self.config.role, attempt + 1, kind.value, delay_ms, exc,
        )
