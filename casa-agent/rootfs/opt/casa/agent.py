"""Core Agent class -- orchestrates SDK, memory, sessions, and channels."""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ProcessError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from plugins_binding import build_sdk_plugins
from plugin_grants import derived_plugin_grants, make_fail_closed_can_use_tool

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager
from config import AgentConfig
from specialist_registry import DelegationComplete
from hooks import resolve_hooks
from log_cid import cid_var
import sdk_logging
from mcp_registry import McpServerRegistry
from channel_trust import channel_trust_display, user_peer_for_channel
from timekeeping import resolve_tz
from hindsight_ids import bank_id
from sensitivity import clearance_for_channel, readable_tiers
from session_saver import freshness_window, retain_cold_session, save_session
from semantic_memory import NoOpSemanticMemory, SemanticMemory
from session_registry import SessionRegistry, build_session_key
from session_sweeper import _is_uuid_scope
from sdk_client_pool import (
    ManagedSdkClient,
    PoolUnavailable,
    SdkClientPool,
    pool_enabled,
)
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
active_executor_registry = None   # ExecutorRegistry | None, set by casa_core.main
active_claude_code_driver = None  # ClaudeCodeDriver | None, set by casa_core.main
active_runtime = None             # CasaRuntime | None, set by casa_core.main (Task C.3)
active_semantic_memory = None     # SemanticMemory | None, set by casa_core.main

# Phase 3.1: delegating-turn origin. Set by Agent._process for the
# duration of a turn so the `delegate_to_agent` tool handler can read
# the channel/chat_id/cid/role/user_text of the outer user turn without
# threading them through function arguments. ContextVar semantics —
# asyncio.create_task snapshots the value, so late-completing specialist
# tasks still see the right origin.
origin_var: ContextVar[dict | None] = ContextVar("origin_var", default=None)

# Type alias for the streaming callback
OnTokenCallback = Callable[[str], Awaitable[None]]


def _render_delegates_block(delegates, registry) -> str:
    """Render the <delegates> system-prompt block.

    Empty string when ``delegates`` is empty so callers can append
    unconditionally without polluting the prompt.
    """
    if not delegates:
        return ""
    # A delegates.yaml entry may point at a specialist that is now disabled
    # (enabled: false) or removed. Such an agent is NOT callable — the
    # delegate_to_agent tool rejects it with unknown_agent — so do not advertise
    # it. The registry holds exactly residents + ENABLED specialists; filter on
    # that. (No registry → back-compat: render every declared delegate.)
    visible = [
        d for d in delegates
        if registry is None or registry.is_known(d.agent)
    ]
    if not visible:
        return ""
    lines = ["<delegates>"]
    for d in visible:
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


def _resume_decision(
    channel: str, entry: dict | None, now: datetime,
) -> tuple[str, bool]:
    """Spec §3.3/§4.2: resume iff an entry exists and is within its channel
    freshness window; otherwise start new — and if a stale entry with a live
    sdk_session_id exists, signal save-before-overwrite (next-turn-after-gap).
    Returns (decision, save_old): decision in {"resume","new"}; save_old True
    only when starting new over a stale-but-present session."""
    if not entry or not entry.get("sdk_session_id"):
        return ("new", False)
    la = entry.get("last_active")
    try:
        last = datetime.fromisoformat(la) if isinstance(la, str) else None
    except ValueError:
        last = None
    if last is not None and (now - last) <= freshness_window(channel):
        return ("resume", False)
    return ("new", True)


@dataclass(frozen=True)
class _LoadPlan:
    push_overlay: bool   # GET mental-model overlay precedes the turn
    auto_recall: bool    # auto-run a query-specific recall on the opening utterance


def _plan_load(channel: str, *, is_fresh_session: bool) -> _LoadPlan:
    """Spec §4.3 channel-aware load. Overlay is pushed only at fresh-session-start
    (it rides along on resume). Voice never auto-recalls (the multi-strategy + rerank
    recall must not sit on the first-utterance critical path); voice uses the
    recall_memory pull tool instead. Note: even when push_overlay is True the overlay
    is additionally gated by ``_overlay_allowed(channel)`` at the call site — a channel
    without ``private`` clearance (e.g. voice = friends) never actually receives the
    overlay regardless of this plan."""
    if not is_fresh_session:
        return _LoadPlan(push_overlay=False, auto_recall=False)
    if channel == "voice":
        return _LoadPlan(push_overlay=True, auto_recall=False)
    return _LoadPlan(push_overlay=True, auto_recall=True)


def _memory_bank() -> str:
    """The single shared long-term bank (design §2.1). Role no longer partitions
    memory — sensitivity tiers do."""
    return bank_id("casa")


def _recall_tier_tags(channel: str) -> list[str]:
    """Tiers a turn on ``channel`` may recall = readable_tiers(clearance). The sole
    read-side access gate (design §2.3)."""
    return readable_tiers(clearance_for_channel(channel))


def _overlay_allowed(channel: str) -> bool:
    """The bank-level mental-model overlay cannot be tier-filtered, so it is pushed
    ONLY at ``private`` clearance — a context that may already see everything
    (design §2.3). At any lower clearance it would leak across tiers."""
    return clearance_for_channel(channel) == "private"


class Agent:
    """A Casa agent backed by the Claude Agent SDK."""

    def __init__(
        self,
        config: AgentConfig,
        session_registry: SessionRegistry,
        mcp_registry: McpServerRegistry,
        channel_manager: ChannelManager,
        agent_registry=None,
        semantic_memory: SemanticMemory | None = None,
    ) -> None:
        self.config = config
        self._semantic_memory: SemanticMemory = semantic_memory or NoOpSemanticMemory()
        self._session_registry = session_registry
        self._mcp_registry = mcp_registry
        self._channel_manager = channel_manager
        self._agent_registry = agent_registry
        self._bg_tasks: set[asyncio.Task] = set()  # background cold-session retains
        # Per-(session_id) over-budget streak tracker (spec 5.2 §5.2).
        # Per-instance so assistant (4000) and butler (800) budgets stay
        # isolated even when the same channel serves both roles.
        self._budget_tracker = BudgetTracker()
        # H2/M20: per-instance cache of the SDK plugin list. build_sdk_plugins
        # shells out to `claude plugin list --json` (a blocking Node spawn);
        # resolving it once per Agent instead of once per turn removes that
        # cost from the hot path. The install.md doctrine makes
        # casa_reload(scope='agent', role=...) MANDATORY after any plugin
        # install/uninstall, and reload_agent constructs a FRESH Agent
        # (reload._construct_agent) — so a per-instance cache never serves a
        # stale plugin set. The lock guards concurrent turns of the same
        # Agent from racing the first (off-loop) resolve.
        self._sdk_plugins: list[dict[str, str]] | None = None
        self._sdk_plugins_lock = asyncio.Lock()
        # Resolve hooks once at construction. HooksConfig.pre_tool_use
        # empty → default policy bundle (block_dangerous_bash + path_scope
        # scoped to cfg.cwd).
        self._resolved_hooks = resolve_hooks(
            config.hooks, default_cwd=config.cwd,
        )

        # Warm SDK-client pool (spec 2026-07-11, AR-1..AR-10). One warm
        # conversation per channel_key, reconciled against the SessionRegistry
        # (the registry stays authoritative — the pool derives the resume
        # decision from a fresh read INSIDE turn() via ``_resume_decision``).
        # ``engagement_var`` is imported LAZILY here to avoid the tools↔agent
        # circular import (tools imports agent only inside functions); resident
        # turns never run inside an engagement binding, so open() asserts it is
        # None. The reset listener gives the pool a chance to close (flush) a
        # key's warm subprocess when the registry is explicitly reset (AR-4).
        from tools import engagement_var as _engagement_var
        self._engagement_var = _engagement_var
        self._pool = SdkClientPool(
            session_registry, decide=_resume_decision,
            origin_ctxvar=origin_var, cid_ctxvar=cid_var,
            engagement_ctxvar=_engagement_var,
        )
        self._unsub_reset = session_registry.add_reset_listener(
            self._pool.close_key,
        )

        # Layer-5 capability boot log: one INFO line per Agent construction
        # (boot AND reload — reload._construct_agent builds a fresh Agent), so
        # a capability regression (a tool grant vanishing after a config_sync
        # reconcile, an MCP server going undeclared) is visible in `docker
        # logs` and diffable across deploys. Logs the CONFIGURED surface
        # (config.tools.allowed) — the thing that drifts vs runtime.yaml; the
        # per-turn auto-appended "Skill" is not a config grant and is omitted.
        # Best-effort: an observability line must never break construction.
        try:
            allowed = list(getattr(config.tools, "allowed", []) or [])
            logger.info(
                "agent_capabilities role=%s model=%s enabled=%s tool_count=%d "
                "tools=%s mcp_servers=%s",
                config.role, getattr(config, "model", "?"),
                getattr(config, "enabled", "?"),
                len(allowed), sorted(allowed),
                sorted(getattr(config, "mcp_server_names", []) or []),
            )
        except Exception:  # noqa: BLE001 — never let the boot log break boot
            logger.warning("agent_capabilities log failed for role=%s",
                           getattr(config, "role", "?"), exc_info=True)

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
        elif channel is not None and hasattr(channel, "turn_finished"):
            # L7 (v0.52.0): a turn that strips to empty / `<silent/>` never
            # calls send()/finalize_stream(), so give the channel a chance to
            # tear down per-turn state (e.g. the Telegram typing indicator).
            # hasattr-guarded so channels without the hook are unaffected;
            # teardown must never break the turn.
            try:
                await channel.turn_finished(msg.context)
            except Exception:  # noqa: BLE001
                logger.exception("channel.turn_finished failed")

        if not text and error_kind is None and msg.type != MessageType.REQUEST:
            return None
        # M4 (v0.53.0): REQUEST turns must ALWAYS return a RESPONSE (possibly
        # empty-content). Voice SSE/WS (channels/voice/channel.py) and /invoke
        # (casa_core.py) block on bus.request(timeout=300); a None return here
        # would leave their pending future unresolved for the full window.
        # Channel delivery of the empty text was already suppressed above
        # (send()/finalize_stream() are skipped, turn_finished() torn down).
        # Note: the delegation-synthesis path rebinds ``msg`` to REQUEST, so an
        # empty delegation turn now returns an empty RESPONSE too — but
        # bus._dispatch keys off the ORIGINAL message (a NOTIFICATION with no
        # pending future), so that RESPONSE is simply ignored.

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

        # The origin snapshot is set into ``origin_var`` for this task (so the
        # delegate_to_agent tool handler can read the outer turn) AND handed to
        # the pool / bypass client as ``origin=`` (the warm client rewrites its
        # read-task-visible holder from it per turn — spec Q7). Same content on
        # both seams.
        origin_snapshot = {
            "role": self.config.role,
            "channel": msg.channel,
            "chat_id": msg.context.get("chat_id", ""),
            # Bug 8 (v0.14.6): propagated through engagement.origin so the
            # Telegram channel can verify the user that issues /cancel or
            # /complete in an engagement topic actually owns the engagement.
            "user_id": msg.context.get("user_id"),
            "cid": cid_var.get(),
            "user_text": user_text,
        }
        origin_token = origin_var.set(origin_snapshot)
        try:
            # Resolve cwd to the agent-home (Plan 4b §5.1). Residents live at
            # /config/agent-home/<role>/; configured cwd on
            # Config stays as an override for legacy tests.
            agent_home = (
                self.config.cwd
                or f"/config/agent-home/{self.config.role}"
            )

            # <current_time> rides on the per-turn query text (NOT the cached
            # system prompt) so the agent still knows the wall-clock time to
            # second precision without busting prompt caching (M27). user_text
            # itself stays raw (it also feeds origin_var + the recall query).
            _now = datetime.now(resolve_tz())
            prompt_text = (
                f"<current_time>\n"
                f"{_now.isoformat(timespec='seconds')} "
                f"({_now.strftime('%A').lower()} "
                f"{_now.strftime('%p').lower()}, "
                f"week {_now.isocalendar().week})\n"
                f"</current_time>\n\n"
                f"{user_text}"
            )

            # Eligibility gate (spec §4, AR-6/AR-7): a pooled warm turn iff the
            # pool is enabled, the turn is not a SCHEDULED heartbeat, and it is
            # not a webhook one-shot (random-uuid chat_id). Ineligible turns —
            # and any PoolUnavailable — fall to the per-turn bypass path, which
            # reproduces today's semantics exactly (decision here → one-shot
            # ManagedSdkClient → aclose in finally).
            use_pool = (
                pool_enabled()
                and msg.type != MessageType.SCHEDULED          # AR-6
                and not (
                    msg.channel == "webhook"
                    and _is_uuid_scope(str(msg.context.get("chat_id", "")))
                )
            )

            # The pool decides the resume sid internally (AR-3), but the
            # ProcessError fallback below needs to know whether the failed
            # attempt was resuming. Both attempt closures record it here.
            last_resume: dict[str, str | None] = {"sid": None}

            async def _attempt_pooled_turn():
                on_message, state = self._make_on_message(on_token)

                async def _build(is_fresh, resume_sid):
                    # Recorded HERE too (not just via on_decision below) so a
                    # ProcessError raised at connect — the resume-failure
                    # class — still tells the fallback below which sid was in
                    # play. Harmless double-set: on_decision already recorded
                    # the same resume_sid a moment earlier, under the entry
                    # lock, before this cold-connect branch even runs.
                    last_resume["sid"] = resume_sid
                    return await self._build_options(
                        channel=msg.channel, channel_key=channel_key,
                        is_fresh=is_fresh, resume_sid=resume_sid,
                        user_text=user_text,
                    )

                result = await self._pool.turn(
                    channel_key=channel_key, channel=msg.channel,
                    prompt=prompt_text, origin=origin_snapshot,
                    cid=cid_var.get(), build_options=_build,
                    on_stale_old=lambda old_sid: self._spawn_cold_retain(
                        old_sid, agent_home, user_peer, msg.channel,
                    ),
                    on_message=on_message,
                    # Finding 2 (final-review): fires for EVERY turn (warm
                    # reuse included), unlike _build above which the pool
                    # skips on warm reuse — so a non-retryable failure on a
                    # warm-reuse turn still leaves last_resume["sid"]
                    # populated for the ProcessError fallback below.
                    on_decision=lambda resume_sid, is_fresh: (
                        last_resume.__setitem__("sid", resume_sid)
                    ),
                )
                last_resume["sid"] = result.resume_sid
                return (
                    state["text"], result.sid, state["usage"],
                    result.resume_sid,
                )

            async def _attempt_bypass_turn():
                # Per-turn path (today's semantics): decision here, one-shot
                # ManagedSdkClient reusing the same turn body.
                existing = self._session_registry.get(channel_key)
                decision, save_old = _resume_decision(
                    msg.channel, existing, datetime.now(timezone.utc),
                )
                resume_sid = (
                    existing.get("sdk_session_id")
                    if decision == "resume" and existing else None
                )
                last_resume["sid"] = resume_sid
                if decision == "resume":
                    await self._session_registry.touch(channel_key)
                elif save_old and (existing or {}).get("sdk_session_id"):
                    # next-turn-after-gap: register() below overwrites this
                    # channel's pointer, so retain the OLD sid in the BACKGROUND
                    # (claim-free / registry-decoupled — cannot race register();
                    # per-item classification runs off the hot path, tier §2.4).
                    self._spawn_cold_retain(
                        existing["sdk_session_id"], agent_home, user_peer,
                        msg.channel,
                    )
                # else ("new", False): no prior entry → nothing to save
                options = await self._build_options(
                    channel=msg.channel, channel_key=channel_key,
                    is_fresh=resume_sid is None, resume_sid=resume_sid,
                    user_text=user_text,
                )
                client = ManagedSdkClient(
                    options, origin_ctxvar=origin_var,
                    cid_ctxvar=cid_var, engagement_ctxvar=self._engagement_var,
                )
                on_message, state = self._make_on_message(on_token)
                try:
                    await client.open()
                    async with client.lock:
                        sid = await client.run_turn_locked(
                            prompt_text, origin=origin_snapshot,
                            cid=cid_var.get(), on_message=on_message,
                        )
                finally:
                    await client.aclose()
                return state["text"], sid, state["usage"], resume_sid

            # Retry transient faults (spec 5.2 §3). The pooled path may raise
            # PoolUnavailable (pool closing / entry unstable) — fall to the
            # per-turn bypass. ProcessError on a resuming attempt = the stale
            # resume class (spec 5.8): clear + retry fresh (the pool re-derives
            # a FRESH decision from the cleared registry).
            attempt = _attempt_pooled_turn if use_pool else _attempt_bypass_turn
            try:
                response_text, sdk_session_id, usage, used_resume = \
                    await retry_sdk_call(attempt, on_retry=self._log_retry)
            except PoolUnavailable:
                response_text, sdk_session_id, usage, used_resume = \
                    await retry_sdk_call(
                        _attempt_bypass_turn, on_retry=self._log_retry,
                    )
            except ProcessError as exc:
                if last_resume["sid"] is None:
                    raise
                # Phase 4b Bug 5: structured retry telemetry. exc.stderr is
                # populated by Bug 4's stderr callback; truncate to a 200-char
                # tail with newlines escaped so one log line stays scannable.
                stderr_tail = (exc.stderr or "")[-200:].replace("\n", "\\n")
                logger.info(
                    "sdk_retry_fresh channel_key=%s exit_code=%s prior_sid=%s "
                    "stderr_tail=%s",
                    channel_key, exc.exit_code, last_resume["sid"], stderr_tail,
                )
                logger.warning(
                    "SDK resume failed (key=%s sid=%s); clearing and retrying "
                    "fresh", channel_key, last_resume["sid"],
                )
                await self._session_registry.clear_sdk_session(channel_key)
                response_text, sdk_session_id, usage, used_resume = \
                    await retry_sdk_call(attempt, on_retry=self._log_retry)

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

            if sdk_session_id and sdk_session_id != used_resume:
                logger.info(
                    "SDK session for '%s': %s",
                    self.config.role,
                    sdk_session_id,
                )

            # 8. Persist — the freshness reaper classifies each retained item at
            # its true sensitivity tier off the critical path (tier model §2.4);
            # nothing to compute or record per-turn here.

            # 9. SessionRegistry — record the SDK session id for resume + save.
            if sdk_session_id:
                await self._session_registry.register(
                    channel_key=channel_key,
                    agent=self.config.role,
                    sdk_session_id=sdk_session_id,
                )

            return response_text or None
        finally:
            origin_var.reset(origin_token)

    async def _build_options(
        self, *, channel: str, channel_key: str, is_fresh: bool,
        resume_sid: str | None, user_text: str,
    ) -> ClaudeAgentOptions:
        """Assemble the connect-time ClaudeAgentOptions for one conversation
        (spec §4.3 steps 2b–6). Extracted verbatim from _process so BOTH the
        pooled path (via the pool's build_options callback) and the bypass path
        build identical options. Keys the channel-aware memory load on the
        ``is_fresh`` PARAMETER (the pool decides it under the entry lock — AR-3),
        not a locally-recomputed value. Returns the stderr-wrapped options."""
        agent_home = (
            self.config.cwd
            or f"/config/agent-home/{self.config.role}"
        )

        # 2b. Memory context (spec §4.3) — channel-aware load on the
        # SemanticMemory seam: a cheap mental-model overlay at fresh-session
        # start, plus (text only) one recall filtered by channel clearance tier.
        load_plan = _plan_load(channel, is_fresh_session=is_fresh)
        bank = _memory_bank()
        overlay_digest = ""
        facts = ""
        if load_plan.push_overlay and _overlay_allowed(channel):
            try:
                overlay_digest = await self._semantic_memory.profile(bank)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "profile overlay failed for role=%s", self.config.role,
                    exc_info=True,
                )
        if load_plan.auto_recall:
            try:
                facts = await self._semantic_memory.recall(
                    bank, user_text, tags=_recall_tier_tags(channel),
                    max_tokens=self.config.memory.token_budget,
                    budget="mid",  # auto_recall is always non-voice (see _plan_load); voice uses the recall_memory pull tool at budget=low
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "recall failed for role=%s", self.config.role, exc_info=True,
                )
        parts: list[str] = []
        if overlay_digest:
            parts.append(f"<peer_overlay>\n{overlay_digest}\n</peer_overlay>")
        if facts:
            parts.append(f"<memory_context>\n{facts}\n</memory_context>")
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
            f"channel: {channel}\n"
            f"trust: {channel_trust_display(channel)}\n"
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
        # NOTE: <current_time> is intentionally NOT part of the system prompt —
        # a per-second timestamp in the cached prefix would invalidate Anthropic
        # prompt caching for the whole conversation every turn (see M27). It
        # rides on the per-turn query text (built in _process).
        system_prompt = "\n".join(system_parts)

        # 4. MCP servers.
        mcp_servers = self._mcp_registry.resolve(self.config.mcp_server_names)

        # 5. Hooks — resolved from hooks.yaml at load time by agent_loader.
        hooks = self._resolved_hooks

        # Binding layer — SDK does NOT auto-consume enabledPlugins; we build the
        # `plugins=[...]` list from `claude plugin list --json`. Resolved
        # off-loop + cached per instance (see _get_sdk_plugins).
        sdk_plugins = await self._get_sdk_plugins()

        # "Skill" is a valid allowed_tools entry (spike §Key learning 4);
        # append if not already declared so plugin-shipped skills resolve.
        allowed_tools = list(self.config.tools.allowed)
        if "Skill" not in allowed_tools:
            allowed_tools.append("Skill")

        # P-5a: installed ⇒ granted, by construction — server-level grants
        # derived from this agent-home's enabledPlugins. Off-loop (H2/M20):
        # settings.json + one .mcp.json per enabled plugin — small reads,
        # but the loop is shared.
        for grant in await asyncio.to_thread(derived_plugin_grants, agent_home):
            if grant not in allowed_tools:
                allowed_tools.append(grant)

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
            resume=resume_sid,
            setting_sources=["project"],
            plugins=sdk_plugins,
            # P-5b: in-casa agents have no permission relay — fail closed on
            # ungranted tools instead of hanging on CC's prompt. New closure
            # per build is fine: the pool reuses clients, not options objects.
            can_use_tool=make_fail_closed_can_use_tool(self.config.role),
            # Voice partial-message streaming (2026-07-11 design §2 point 1):
            # SDK partial StreamEvents are opt-in and constant per channel,
            # so this stays pool-key compatible (spec §Q6). Non-voice
            # channels are byte-for-byte unaffected — StreamEvents simply
            # never arrive when this is False.
            include_partial_messages=(channel == "voice"),
        )
        return sdk_logging.with_stderr_callback(options, engagement_id=None)

    def _make_on_message(self, on_token: OnTokenCallback | None):
        """Build the per-turn ``on_message(sdk_msg)`` handler + its ``state``.

        Reproduces today's per-message body VERBATIM (Phase 4b sdk_logging
        dispatch wrapped in try/except, tool_names_by_id, idx counter,
        started_ms, E-2 streaming concat into state["text"] with cumulative
        on_token, usage extraction into state["usage"]) — MINUS session-id
        capture, which the warm client / pool now owns (spec Q7). A fresh state
        per call resets the streaming accumulator per attempt (spec §3.2).

        Voice partial-message streaming (2026-07-11 design, AR-A/AR-B/AR-E):
        ``state["partial"]`` accumulates the in-flight message's text deltas
        from ``StreamEvent`` messages (only ever produced when
        ``include_partial_messages=True``, i.e. voice turns — see
        ``_build_options``). ``_cum()`` is the single pinned formula (AR-A)
        joining the folded ``state["text"]`` to the in-flight partial with
        the same "\\n\\n" separator the canonical fold uses, so a partial
        emission and the eventual fold never disagree — this is what makes
        message N+1's FIRST partial emission already carry the joiner.
        ``state["last_emitted"]`` dedupes: on_token only fires when the
        computed cumulative actually changed (AR-A/AR-B)."""
        state: dict[str, Any] = {
            "text": "",
            "usage": {},
            "idx": 0,
            "started_ms": time.monotonic() * 1000,
            "tool_names_by_id": {},
            "partial": "",
            "last_emitted": "",
        }

        def _cum() -> str:
            return (
                state["text"]
                + ("\n\n" if state["text"] and state["partial"] else "")
                + state["partial"]
            )

        async def on_message(sdk_msg: Any) -> None:
            # Voice partial streaming — handled EARLY so a StreamEvent never
            # falls through to the phase4b dispatch below (no per-token log
            # lines, no idx/tool bookkeeping). AR-E: defensive parsing — the
            # CLI can forward raw `error` events with no `delta` key, or any
            # other shape; a malformed event must never abort the turn.
            if isinstance(sdk_msg, StreamEvent):
                try:
                    ev = getattr(sdk_msg, "event", None) or {}
                    if ev.get("type") == "content_block_delta":
                        d = ev.get("delta") or {}
                        if d.get("type") == "text_delta":
                            t = d.get("text") or ""
                            if t:
                                state["partial"] += t
                                cum = _cum()
                                if (
                                    on_token is not None
                                    and cum != state["last_emitted"]
                                ):
                                    await on_token(cum)
                                    state["last_emitted"] = cum
                except Exception as stream_exc:  # noqa: BLE001
                    logger.warning(
                        "stream_event dispatch failed: %s", stream_exc,
                        exc_info=True,
                    )
                return

            # Phase 4b dispatch — wrapped so a malformed block cannot abort the
            # turn (logged + continued).
            try:
                if isinstance(sdk_msg, SystemMessage):
                    sdk_logging.log_system_init(sdk_msg)
                elif isinstance(sdk_msg, AssistantMessage):
                    state["idx"] += 1
                    sdk_logging.log_assistant_message(sdk_msg, idx=state["idx"])
                    for block in getattr(sdk_msg, "content", []) or []:
                        if isinstance(block, ToolUseBlock):
                            state["tool_names_by_id"][
                                getattr(block, "id", "")
                            ] = getattr(block, "name", "?")
                            sdk_logging.log_tool_use(block, idx=state["idx"])
                elif isinstance(sdk_msg, UserMessage):
                    for block in getattr(sdk_msg, "content", []) or []:
                        if isinstance(block, ToolResultBlock):
                            name = state["tool_names_by_id"].get(
                                getattr(block, "tool_use_id", ""), "",
                            )
                            sdk_logging.log_tool_result(
                                block, idx=state["idx"],
                                started_ms=state["started_ms"], name=name,
                            )
                elif isinstance(sdk_msg, ResultMessage):
                    sdk_logging.log_turn_done(
                        sdk_msg, started_ms=state["started_ms"],
                    )
            except Exception as dispatch_exc:  # noqa: BLE001
                logger.warning(
                    "phase4b dispatch failed: %s", dispatch_exc, exc_info=True,
                )
            # Usage + E-2 streaming concat (session-id capture is the client's).
            if isinstance(sdk_msg, ResultMessage):
                state["usage"] = extract_usage(sdk_msg)
            elif isinstance(sdk_msg, AssistantMessage):
                # E-2: collect TextBlocks of THIS AssistantMessage.
                msg_text = "".join(
                    b.text for b in getattr(sdk_msg, "content", [])
                    if isinstance(b, TextBlock)
                )
                if msg_text:
                    if state["text"]:
                        state["text"] += "\n\n"
                    state["text"] += msg_text
                # The canonical fold supersedes any in-flight partial for
                # this message (AR-A/AR-B): reset before computing the
                # cumulative so a stale partial never bleeds into message
                # N+1's first delta. Runs unconditionally (even when this
                # message carried no text, e.g. tool-use-only) so a
                # tool-only fold never leaves a stale partial dangling —
                # cum() then equals state["text"] unchanged, which already
                # matches last_emitted, so no spurious emit follows.
                state["partial"] = ""
                cum = _cum()
                if on_token is not None and cum != state["last_emitted"]:
                    await on_token(cum)
                    state["last_emitted"] = cum

        return on_message, state

    async def aclose(self) -> None:
        """Release pooled SDK clients + reset hook. Safe to call twice; called
        by reload (old instance) and casa_core shutdown."""
        try:
            self._unsub_reset()
        except Exception:  # noqa: BLE001
            pass
        await self._pool.aclose()

    async def _get_sdk_plugins(self) -> list[dict[str, str]]:
        """Resolve this resident's SDK ``plugins=[...]`` list, off-loop + cached.

        ``build_sdk_plugins`` runs ``claude plugin list --json`` — a blocking
        Node-CLI spawn. Calling it inline on every turn froze the single shared
        event loop (H2/M20); here it runs via ``asyncio.to_thread`` so no other
        channel/agent stalls, and the result is cached on the instance so the
        cost is paid once per Agent, not once per turn.

        Cache invalidation is by Agent reconstruction: the install.md doctrine
        makes ``casa_reload(scope='agent', role=...)`` mandatory after any
        plugin install/uninstall, and ``reload._construct_agent`` builds a fresh
        Agent — so this cache can never surface a stale plugin set. A degraded
        empty result (build_sdk_plugins' CLI-failure fallback) is deliberately
        NOT cached, so the next turn retries — preserving plugins_binding.py's
        documented recovery policy.
        """
        if self._sdk_plugins is not None:
            return self._sdk_plugins
        async with self._sdk_plugins_lock:
            if self._sdk_plugins is not None:
                return self._sdk_plugins
            plugins = await asyncio.to_thread(
                build_sdk_plugins,
                home="/config/cc-home",
                shared_cache="/config/cc-home/.claude/plugins",
                seed="/opt/claude-seed",
                role=self.config.role,
            )
            if plugins:
                self._sdk_plugins = plugins
            return plugins

    def _spawn_cold_retain(
        self, sid: str, directory: str, user_peer: str, channel: str,
    ) -> None:
        """Retain a cold prior session in the background (claim-free; cannot race
        register()). Tracked so it isn't GC'd; failures are swallowed in
        retain_cold_session and never reach the turn."""
        task = asyncio.create_task(
            retain_cold_session(
                sid=sid, role=self.config.role, directory=directory,
                user_peer=user_peer, channel=channel,
                semantic_memory=self._semantic_memory,
            ),
            name=f"cold-retain-{sid}",
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _log_retry(self, attempt: int, exc: Exception, delay_ms: int) -> None:
        """Emit a single WARNING per retry event (spec 5.2 §3.2)."""
        kind = _classify_error(exc)
        logger.warning(
            "SDK retry: role=%s attempt=%d kind=%s delay_ms=%d exc=%r",
            self.config.role, attempt + 1, kind.value, delay_ms, exc,
        )
