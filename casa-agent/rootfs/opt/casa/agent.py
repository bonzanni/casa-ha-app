"""Core Agent class -- orchestrates SDK, memory, sessions, and channels."""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from scope_registry import ScopeRegistry

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from plugins_binding import build_sdk_plugins

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from config import AgentConfig
from specialist_registry import DelegationComplete
from hooks import resolve_hooks
from log_cid import cid_var
import sdk_logging
from mcp_registry import McpServerRegistry
from channel_trust import channel_trust, channel_trust_display, user_peer_for_channel
from timekeeping import resolve_tz
from memory import MemoryProvider
from honcho_ids import honcho_session_id
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

# Module-level driver/provider/registry references written by casa_core.main
# so tool handlers can reach them without circular imports.
active_engagement_driver = None   # InCasaDriver | None, set by casa_core.main
active_memory_provider = None     # MemoryProvider | None, set by casa_core.main
active_executor_registry = None   # ExecutorRegistry | None, set by casa_core.main
active_claude_code_driver = None  # ClaudeCodeDriver | None, set by casa_core.main
active_runtime = None             # CasaRuntime | None, set by casa_core.main (Task C.3)

# Phase 3.1: delegating-turn origin. Set by Agent._process for the
# duration of a turn so the `delegate_to_agent` tool handler can read
# the channel/chat_id/cid/role/user_text of the outer user turn without
# threading them through function arguments. ContextVar semantics —
# asyncio.create_task snapshots the value, so late-completing specialist
# tasks still see the right origin.
origin_var: ContextVar[dict | None] = ContextVar("origin_var", default=None)

# Type alias for the streaming callback
OnTokenCallback = Callable[[str], Awaitable[None]]


def _winner_pair(scores: dict[str, float]) -> tuple[str | None, float, float]:
    """Pick (winner, winner_score, second_score) from a scope-score dict.

    Returns (None, 0.0, 0.0) when scores is empty (e.g., trust filter
    pruned every readable scope).
    """
    if not scores:
        return None, 0.0, 0.0
    sorted_pairs = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    winner, winner_score = sorted_pairs[0]
    second_score = sorted_pairs[1][1] if len(sorted_pairs) > 1 else 0.0
    return winner, float(winner_score), float(second_score)


def _render_delegates_block(delegates, registry) -> str:
    """Render the <delegates> system-prompt block.

    Empty string when ``delegates`` is empty so callers can append
    unconditionally without polluting the prompt.
    """
    if not delegates:
        return ""
    lines = ["<delegates>"]
    for d in delegates:
        name = registry.role_to_name(d.agent) if registry is not None else d.agent
        lines.append(f"- {name} (role: {d.agent}) — {d.purpose}")
        lines.append(f"  Delegate when: {d.when}")
    lines.append("</delegates>")
    return "\n".join(lines)


def _render_executors_block(executors) -> str:
    """Render the <executors> system-prompt block (assistant role only)."""
    if not executors:
        return ""
    lines = ["<executors>"]
    for e in executors:
        lines.append(f"- {e.executor_type} — {e.purpose}")
        lines.append(f"  Engage when: {e.when}")
    lines.append("</executors>")
    return "\n".join(lines)


class Agent:
    """A Casa agent backed by the Claude Agent SDK."""

    def __init__(
        self,
        config: AgentConfig,
        memory: MemoryProvider,
        session_registry: SessionRegistry,
        mcp_registry: McpServerRegistry,
        channel_manager: ChannelManager,
        scope_registry: "ScopeRegistry",
        agent_registry=None,
    ) -> None:
        self.config = config
        self._memory = memory
        self._session_registry = session_registry
        self._mcp_registry = mcp_registry
        self._channel_manager = channel_manager
        self._scope_registry = scope_registry
        self._agent_registry = agent_registry
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

        # Obtain a streaming callback from the channel (if available).
        # SCHEDULED turns are buffered: the agent thinks privately and
        # only the final text is sent. This prevents Telegram leaking
        # acknowledgement-style first tokens into the chat before the
        # prompt's silence check completes (spec 2026-04-28 §3.2 B.1).
        on_token: OnTokenCallback | None = None
        channel = self._channel_manager.get(msg.channel) if msg.channel else None

        if (
            channel is not None
            and hasattr(channel, "create_on_token")
            and msg.type != MessageType.SCHEDULED
        ):
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

        # Silence sentinel suppression — applies to ALL message types.
        #
        # Origin contract (spec 2026-04-28 §3.2 B.2): the agent's tokens
        # for SCHEDULED turns are buffered by Fix B.1, so the prompt can
        # signal "do not send" via the literal sentinel `<silent/>` or
        # by producing only whitespace. Strict, exact-match-after-strip
        # — substring matches are rejected by design (prompt produces
        # `<silent/>` followed by recanting text → send the whole thing).
        #
        # G-3 (v0.33.0, exploration2): the suppression was originally
        # scoped to `msg.type == MessageType.SCHEDULED`. Ellen's outer
        # USER-driven turn after a configurator engagement (cid
        # `dcc3c30b` 2026-05-01) accidentally absorbed the heartbeat
        # trigger's `<silent/>` doctrine via mid-engagement Read of
        # triggers.yaml and then emitted the bare sentinel as her own
        # noop on the user DM, where this gate didn't fire. Lifting the
        # SCHEDULED-only condition makes the behavior consistent: any
        # turn whose entire output strips to `<silent/>` (or to
        # whitespace) is a no-op, regardless of channel-trigger source.
        # The cost of false suppression is zero in practice (no model
        # legitimately emits the literal sentinel string to a user); the
        # cost of operator-visible literal `<silent/>` is real-but-small.
        if text:
            stripped = text.strip()
            if not stripped or stripped == "<silent/>":
                text = ""

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
        user_peer = user_peer_for_channel(msg.channel)
        user_text = str(msg.content)

        origin_token = origin_var.set({
            "role": self.config.role,
            "channel": msg.channel,
            "chat_id": msg.context.get("chat_id", ""),
            # Bug 8 (v0.14.6): propagated through engagement.origin so the
            # Telegram channel can verify the user that issues /cancel or
            # /complete in an engagement topic actually owns the engagement.
            "user_id": msg.context.get("user_id"),
            "cid": cid_var.get(),
            "user_text": user_text,
        })
        try:
            scope_start_t = time.perf_counter()

            # --- 3.2 scope routing: compute readable × active set ----------
            trust_token = channel_trust(msg.channel)
            readable = self._scope_registry.filter_readable(
                list(self.config.memory.scopes_readable),
                trust_token,
            )
            # M4: Partition. System scopes (kind: system) are always-on after
            # the trust filter — no classifier routing, no embedding lookup.
            # Topical scopes (kind: topical) go through the embedding score()
            # + active_from_scores() pipeline as before.
            system_readable = [
                s for s in readable if self._scope_registry.kind(s) == "system"
            ]
            topical_readable = [
                s for s in readable if self._scope_registry.kind(s) == "topical"
            ]
            scores = self._scope_registry.score(user_text, topical_readable)
            active_topical = self._scope_registry.active_from_scores(
                scores, self.config.memory.default_scope,
            )
            active = system_readable + active_topical
            # M2.G6: argmax over topical-only scores. System scopes have no
            # embedding (no description); they can never be the rooted
            # write/origin scope. Argmax picks the engager's actual domain.
            origin_var.set({
                **(origin_var.get() or {}),
                "scope": self._scope_registry.argmax_scope(
                    scores, self.config.memory.default_scope,
                ),
            })

            # Per-turn budget split: 40% for the deduped peer overlay, 60% for
            # per-scope session reads. Constants per spec § 2.3 (Phase 5 —
            # locked at plan-write Task A.0 against one live Ellen turn
            # baseline).
            overlay_budget = max(int(self.config.memory.token_budget * 0.4), 1)
            per_scope_budget = max(
                (self.config.memory.token_budget - overlay_budget)
                // max(len(active), 1),
                1,
            )

            async def _one_scope(scope: str) -> tuple[str, str]:
                sid = honcho_session_id(channel_key, scope, self.config.role)
                try:
                    await self._memory.ensure_session(
                        session_id=sid,
                        agent_role=self.config.role,
                        user_peer=user_peer,
                    )
                    digest = await self._memory.get_context(
                        session_id=sid,
                        tokens=per_scope_budget,
                        search_query=user_text,
                        # M3-self (v0.30.0): forwarded as Honcho's
                        # peer_target so semantic retrieval is scoped
                        # to this agent's view of the session. Without
                        # this, Honcho 2.1.1 raises ValueError on every
                        # search_query-bearing call.
                        agent_role=self.config.role,
                    )
                except Exception:
                    # E-B (v0.29.0): exc_info=True — without this, the
                    # exception class + message disappear, making it
                    # impossible to root-cause M3-self failures from
                    # production logs. The bug was confirmed firing 5×
                    # per Ellen Telegram turn from v0.x to v0.28.1.
                    logger.warning(
                        "Memory call failed for scope=%s session=%s",
                        scope, sid, exc_info=True,
                    )
                    digest = ""
                return scope, digest

            async def _overlay() -> str:
                """One peer-level overlay read per turn — deduped across
                all scopes by Honcho's peer-aggregation design (spec § 2.2)."""
                try:
                    return await self._memory.peer_overlay_context(
                        observer_role=self.config.role,
                        user_peer=user_peer,
                        search_query=user_text,
                        tokens=overlay_budget,
                    )
                except Exception:
                    # E-B companion: same observability rule applies to
                    # the overlay read.
                    logger.warning(
                        "Peer overlay call failed for observer=%s user_peer=%s",
                        self.config.role, user_peer, exc_info=True,
                    )
                    return ""

            # Run overlay + per-scope reads in parallel.
            overlay_digest, *scope_pairs = await asyncio.gather(
                _overlay(),
                *[_one_scope(s) for s in active],
            )
            digests = dict(scope_pairs)

            # spec § 7 Q4 — log empty overlay so operators can spot the
            # regime (Honcho deriver behind, fresh peer pair, etc.). INFO
            # not WARNING.
            if not overlay_digest:
                logger.info(
                    "peer_overlay_empty",
                    extra={
                        "observer_role": self.config.role,
                        "user_peer": user_peer,
                        "channel_key": channel_key,
                    },
                )

            # Assemble — overlay first (durable identity), scopes last
            # (recent conversation). Per spec § 7 Q2 — defer cache-read
            # measurement to post-deploy; flip ordering in v0.26.x if
            # cache_read regresses.
            parts: list[str] = []
            if overlay_digest:
                parts.append(
                    f"<peer_overlay>\n{overlay_digest}\n</peer_overlay>"
                )
            for scope, digest in digests.items():
                if digest:
                    parts.append(
                        f'<memory_context scope="{scope}">\n{digest}\n</memory_context>'
                    )
            memory_blocks = "\n".join(parts)

            if memory_blocks:
                self._budget_tracker.record(
                    f"{channel_key}-{self.config.role}",
                    estimate_tokens(memory_blocks),
                    self.config.memory.token_budget,
                )

            # 3. System prompt = composed-prompt + runtime-injected blocks.
            system_parts = [self.config.system_prompt]
            if memory_blocks:
                system_parts.append("\n" + memory_blocks)
            system_parts.append(
                "\n<channel_context>\n"
                f"channel: {msg.channel}\n"
                f"trust: {channel_trust_display(msg.channel)}\n"
                "</channel_context>"
            )
            # <delegates> block — renders cfg.delegates with display names.
            delegates_block = _render_delegates_block(
                self.config.delegates, self._agent_registry,
            )
            if delegates_block:
                system_parts.append("\n" + delegates_block)
            # <executors> block — assistant role only (loader enforces this).
            executors_block = _render_executors_block(self.config.executors)
            if executors_block:
                system_parts.append("\n" + executors_block)
            _now = datetime.now(resolve_tz())
            system_parts.append(
                f"\n<current_time>\n"
                f"{_now.isoformat(timespec='seconds')} "
                f"({_now.strftime('%A').lower()} "
                f"{_now.strftime('%p').lower()}, "
                f"week {_now.isocalendar().week})\n"
                f"</current_time>"
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

            # Resolve cwd to the agent-home (Plan 4b §5.1). Residents live at
            # /addon_configs/casa-agent/agent-home/<role>/; configured cwd on
            # Config stays as an override for legacy tests.
            agent_home = (
                self.config.cwd
                or f"/addon_configs/casa-agent/agent-home/{self.config.role}"
            )

            # Binding layer — SDK does NOT auto-consume enabledPlugins; we
            # build the `plugins=[...]` list from `claude plugin list --json`.
            sdk_plugins = build_sdk_plugins(
                home="/addon_configs/casa-agent/cc-home",
                shared_cache="/addon_configs/casa-agent/cc-home/.claude/plugins",
                seed="/opt/claude-seed",
                role=self.config.role,
            )

            # "Skill" is a valid allowed_tools entry (spike §Key learning 4);
            # append if not already declared so plugin-shipped skills resolve.
            allowed_tools = list(self.config.tools.allowed)
            if "Skill" not in allowed_tools:
                allowed_tools.append("Skill")

            options = ClaudeAgentOptions(
                model=self.config.model,
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                disallowed_tools=self.config.tools.disallowed,
                permission_mode=self.config.tools.permission_mode or "acceptEdits",
                max_turns=self.config.tools.max_turns,
                mcp_servers=mcp_servers if mcp_servers else {},
                hooks=hooks,
                cwd=agent_home,
                resume=resume_session_id,
                setting_sources=["project"],
                plugins=sdk_plugins,
            )

            # 7. Query the SDK — retry transient faults (spec 5.2 §3). --------
            async def _attempt_sdk_turn() -> tuple[str, str | None, dict[str, int]]:
                """Run one end-to-end SDK turn. Phase 4b: every SDK message
                kind dispatches through sdk_logging in addition to the
                E-2 streaming concat below. Each attempt resets the
                streaming accumulator so ``on_token`` delivers cumulative
                text from scratch if an earlier attempt failed mid-turn.
                """
                attempt_text = ""
                attempt_sid: str | None = resume_session_id
                attempt_usage: dict[str, int] = {}
                idx = 0
                started_ms = time.monotonic() * 1000
                tool_names_by_id: dict[str, str] = {}
                async with ClaudeSDKClient(
                    sdk_logging.with_stderr_callback(
                        options, engagement_id=None,
                    ),
                ) as client:
                    await client.query(user_text)
                    async for sdk_msg in client.receive_response():
                        # Phase 4b dispatch — wrapped so a malformed block
                        # cannot abort the turn (logged + continued).
                        try:
                            if isinstance(sdk_msg, SystemMessage):
                                sdk_logging.log_system_init(sdk_msg)
                            elif isinstance(sdk_msg, AssistantMessage):
                                idx += 1
                                sdk_logging.log_assistant_message(sdk_msg, idx=idx)
                                for block in getattr(sdk_msg, "content", []) or []:
                                    if isinstance(block, ToolUseBlock):
                                        tool_names_by_id[
                                            getattr(block, "id", "")
                                        ] = getattr(block, "name", "?")
                                        sdk_logging.log_tool_use(block, idx=idx)
                            elif isinstance(sdk_msg, UserMessage):
                                for block in getattr(sdk_msg, "content", []) or []:
                                    if isinstance(block, ToolResultBlock):
                                        name = tool_names_by_id.get(
                                            getattr(block, "tool_use_id", ""),
                                            "",
                                        )
                                        sdk_logging.log_tool_result(
                                            block, idx=idx, started_ms=started_ms,
                                            name=name,
                                        )
                            elif isinstance(sdk_msg, ResultMessage):
                                sdk_logging.log_turn_done(
                                    sdk_msg, started_ms=started_ms,
                                )
                        except Exception as dispatch_exc:  # noqa: BLE001
                            logger.warning(
                                "phase4b dispatch failed: %s", dispatch_exc,
                                exc_info=True,
                            )
                        # Existing branches — session_id capture, usage,
                        # E-2 streaming concat — unchanged.
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
                            # E-2: collect TextBlocks of THIS AssistantMessage.
                            msg_text = "".join(
                                b.text for b in getattr(sdk_msg, "content", [])
                                if isinstance(b, TextBlock)
                            )
                            if msg_text:
                                if attempt_text:
                                    attempt_text += "\n\n"
                                attempt_text += msg_text
                                if on_token is not None:
                                    await on_token(attempt_text)
                return attempt_text, attempt_sid, attempt_usage

            try:
                response_text, sdk_session_id, usage = await retry_sdk_call(
                    _attempt_sdk_turn, on_retry=self._log_retry,
                )
            except ProcessError as exc:
                # claude CLI exited non-zero. If we were resuming a prior
                # session, the most common cause (spec 5.8) is a stale
                # sdk_session_id — the local conversation file under
                # ``/root/.claude/`` was wiped (rebuild) while
                # ``/data/sessions.json`` persisted. Clear and retry fresh.
                if resume_session_id is None:
                    raise
                # Phase 4b Bug 5: structured retry telemetry.
                # exc.stderr is populated by Bug 4's stderr callback
                # (subprocess_cli.py:472 + ProcessError._errors.py:25-37);
                # truncate to a 200-char tail with newlines escaped so
                # one log line stays scannable.
                stderr_tail = (exc.stderr or "")[-200:].replace("\n", "\\n")
                logger.info(
                    "sdk_retry_fresh channel_key=%s exit_code=%s prior_sid=%s "
                    "stderr_tail=%s",
                    channel_key, exc.exit_code, resume_session_id, stderr_tail,
                )
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

            # 8. Classify + persist — off the critical path. -----------------
            write_scope: str = "-"
            if response_text:
                # Scope-classify the full exchange over (owned ∩ readable).
                # Empty intersection = channel's trust tier forbids every
                # scope the agent owns. Persisting to default_scope would
                # leak the exchange into a scope the channel can't see —
                # skip the write instead.
                owned_and_readable = [
                    s for s in self.config.memory.scopes_owned if s in readable
                ]
                if owned_and_readable:
                    # Skip the classify round-trip when there's only one
                    # candidate — the argmax is trivially that scope, so
                    # a 90 ms ONNX forward pass would buy us nothing.
                    # This is the common butler (voice, owns=[house])
                    # case.
                    if len(owned_and_readable) == 1:
                        write_scope = owned_and_readable[0]
                    else:
                        write_scores = self._scope_registry.score(
                            f"{user_text}\n{response_text}",
                            owned_and_readable,
                        )
                        write_scope = self._scope_registry.argmax_scope(
                            write_scores, self.config.memory.default_scope,
                        )
                    write_sid = honcho_session_id(
                        channel_key, write_scope, self.config.role,
                    )
                    task = asyncio.create_task(self._add_turn_bg(
                        write_sid, self.config.role, user_text, response_text, user_peer,
                    ))
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)

            # --- 3.2 observability ----------------------------------------
            total_ms = int((time.perf_counter() - scope_start_t) * 1000)
            hits, misses = self._scope_registry.cache_stats()
            winner, winner_score, second_score = _winner_pair(scores)
            logger.info(
                "scope_route",
                extra={
                    "channel": msg.channel,
                    "role": self.config.role,
                    "active": list(active),
                    "write": write_scope,
                    "winner": winner,
                    "winner_score": winner_score,
                    "second_score": second_score,
                    "threshold": self._scope_registry.threshold,
                    "t_ms": total_ms,
                    "embed_cache_hits": hits,
                    "embed_cache_total": hits + misses,
                },
            )

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
