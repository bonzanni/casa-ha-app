"""in_casa driver — embedded claude_agent_sdk engagement runtime."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from drivers.driver_protocol import DriverProtocol
from engagement_registry import EngagementRecord
import sdk_logging

if TYPE_CHECKING:
    from channels.telegram import TopicStreamHandle

logger = logging.getLogger(__name__)


TopicStreamFactory = Callable[[int], "TopicStreamHandle"]
"""(topic_id) → TopicStreamHandle — channel-side per-turn streaming primitive.

Returned handle exposes async ``emit(accumulated_text)`` and async
``finalize(full_text)``. See channels.telegram.TopicStreamHandle."""

ResultObserver = Callable[[EngagementRecord, ResultMessage], None]
"""(engagement, ResultMessage) → None — Task 6 (spec §4.6) per-turn cost/usage
observer. Called synchronously for every ``ResultMessage`` seen on an
engagement turn; casa_core wires it to feed interactive specialist cost into
``SpecialistTelemetry`` (filtering by ``engagement.kind``). Must not raise."""

SessionIdPersister = Callable[[str, str], Awaitable[None]]
"""(engagement_id, session_id) → None — registry persist hook.

Matches engagement_registry.persist_session_id's bound-method signature."""


def _session_id_from_message(sdk_msg: Any) -> str | None:
    """Extract the CLI session id from an SDK message (v0.69.11).

    The pinned Agent SDK's ``ClaudeSDKClient`` exposes no ``session_id``
    attribute; the id arrives in the stream — on the ``SystemMessage`` whose
    ``subtype == "init"`` (``data["session_id"]``) and on every
    ``ResultMessage`` (``.session_id``). Same source ``sdk_client_pool`` uses.
    """
    if isinstance(sdk_msg, SystemMessage):
        if getattr(sdk_msg, "subtype", None) == "init":
            data = getattr(sdk_msg, "data", None) or {}
            sid = data.get("session_id") if isinstance(data, dict) else None
            if isinstance(sid, str) and sid:
                return sid
    elif isinstance(sdk_msg, ResultMessage):
        sid = getattr(sdk_msg, "session_id", None)
        if isinstance(sid, str) and sid:
            return sid
    return None


class DriverNotAliveError(RuntimeError):
    """Raised when a turn is fed to a driver that has no open client."""


class InCasaDriver(DriverProtocol):
    """Holds one ClaudeSDKClient per active engagement.

    ``topic_stream_factory`` is the channel-side factory that, given a
    topic_id, returns a TopicStreamHandle. Each ``_deliver_turn`` builds
    a fresh handle and emits AssistantMessage chunks progressively
    (Phase 3b — Bug 1). Injected rather than imported from
    ``channels.telegram`` to keep the driver pure/testable.
    """

    def __init__(
        self,
        *,
        topic_stream_factory: TopicStreamFactory,
        persist_session_id: SessionIdPersister | None = None,
        result_observer: "ResultObserver | None" = None,
    ) -> None:
        self._topic_stream_factory = topic_stream_factory
        self._persist_session_id = persist_session_id
        # Task 6 (spec §4.6): optional per-turn cost/usage observer.
        self._result_observer = result_observer
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._ctx_stack: dict[str, Any] = {}
        # Per-engagement asyncio.Lock guards query/receive_response sequencing:
        # ClaudeSDKClient is single-threaded per connection.
        self._locks: dict[str, asyncio.Lock] = {}
        # v0.69.11: the CLI's session id, captured from the MESSAGE STREAM
        # (SystemMessage init `data["session_id"]` / ResultMessage.session_id) —
        # ClaudeSDKClient has NO `session_id` attribute on the pinned Agent SDK
        # (0.2.114), so the old getattr(client, "session_id") was always None
        # and persistence silently never fired. Same source the warm pool uses.
        self._session_ids: dict[str, str] = {}

    # -- lifecycle --------------------------------------------------------

    async def start(
        self,
        engagement: EngagementRecord,
        prompt: str,
        options: ClaudeAgentOptions,
    ) -> None:
        # E-E (v0.29.0): bind engagement_var BEFORE ClaudeSDKClient.__aenter__
        # so the SDK's inner Query._read_task — created via loop.create_task
        # in claude_agent_sdk._internal.query.Query.start — captures the
        # engagement in its ContextVar copy. Tool callbacks dispatched from
        # that inner task (and any spawn_task descendants) inherit the
        # binding. Without this, _effective_caller_role() reads None and
        # falls through to origin_var.role ("assistant" — Ellen's bus role
        # via origin_var inherited along the parent task), every privileged
        # tool refuses, and the engagement orphans. Lazy-imported to avoid
        # circular import (tools imports engagement_registry).
        from tools import engagement_var

        assert engagement.topic_id is not None, (
            "in_casa driver requires a topic_id (got None)"
        )
        client = ClaudeSDKClient(
            sdk_logging.with_stderr_callback(
                options, engagement_id=engagement.id[:8],
            ),
        )
        token = engagement_var.set(engagement)
        try:
            ctx = client.__aenter__()
            entered = await ctx if asyncio.iscoroutine(ctx) else ctx
            self._clients[engagement.id] = entered or client
            self._ctx_stack[engagement.id] = client  # for __aexit__
            self._locks[engagement.id] = asyncio.Lock()
            logger.info(
                "Engagement %s driver=in_casa client opened",
                engagement.id[:8],
            )
            try:
                await self._deliver_turn(engagement, prompt)
            except Exception:
                # M14: Bug-13-style rollback (claude_code got this in v0.14.6).
                # engage_executor marks the record error, but error records are
                # excluded from active_and_idle() so no sweeper ever tears this
                # client down, and the topic stops routing — the opened claude
                # subprocess leaks until Casa restarts. Close + deregister here,
                # then re-raise so the caller's mark_error path still runs.
                # cancel() pops _clients/_ctx_stack/_locks and swallows close
                # errors, so the original exception is never masked.
                logger.warning(
                    "Engagement %s first turn failed; rolling back client",
                    engagement.id[:8],
                )
                await self.cancel(engagement)
                raise
        finally:
            # Clear from the parent task. The SDK inner task already
            # captured its own snapshot at __aenter__ time and is
            # unaffected by this reset.
            engagement_var.reset(token)

    async def send_user_turn(
        self, engagement: EngagementRecord, text: str,
        *, tg_message_id: int | None = None,
    ) -> None:
        # tg_message_id is part of the uniform driver interface (v0.79
        # reply-threading); in_casa turns have no topic-stream threading,
        # so it is accepted and ignored.
        if not self.is_alive(engagement):
            raise DriverNotAliveError(
                f"engagement {engagement.id[:8]} has no live client"
            )
        await self._deliver_turn(engagement, text)

    async def cancel(self, engagement: EngagementRecord) -> None:
        client = self._clients.pop(engagement.id, None)
        ctx = self._ctx_stack.pop(engagement.id, None)
        self._locks.pop(engagement.id, None)
        self._session_ids.pop(engagement.id, None)
        if client is None and ctx is None:
            return
        try:
            # Prefer close() on the entered client; fall back to __aexit__ on
            # the original context manager object if close() is absent.
            if client is not None and hasattr(client, "close"):
                await client.close()
            elif ctx is not None and hasattr(ctx, "__aexit__"):
                await ctx.__aexit__(None, None, None)
        except Exception as exc:
            logger.warning(
                "Engagement %s cancel: client close raised %s",
                engagement.id[:8], exc,
            )

    async def resume(
        self, engagement: EngagementRecord, session_id: str,
    ) -> None:
        """Reopen a ClaudeSDKClient with resume=session_id.

        Caller (telegram routing path, after user turn in a suspended topic)
        handles retry + error surfacing. This method raises on failure.
        """
        # E-E (v0.29.0): same propagation requirement as start() — the
        # resumed client also creates a fresh _read_task during __aenter__.
        from tools import engagement_var

        if self.is_alive(engagement):
            logger.warning(
                "resume() called on engagement %s that is already alive",
                engagement.id[:8],
            )
            return
        # Finding 2 (codex review v0.69.10): rebuild the FULL option set (a
        # bare ClaudeAgentOptions(resume=) drops disallowed_tools/Agent+Task,
        # the fail-closed callback, hooks, skills, MCP restrictions — running
        # the resumed engagement unrestricted). Off-loop: the builder reads the
        # registry + hooks.yaml. Fails closed if the config is gone (§3.8: an
        # executor resumes from its recorded plugin artifacts, never re-resolved).
        from tools import build_engagement_resume_options
        options = await asyncio.to_thread(
            build_engagement_resume_options, engagement, session_id,
        )
        client = ClaudeSDKClient(
            sdk_logging.with_stderr_callback(
                options, engagement_id=engagement.id[:8],
            ),
        )
        token = engagement_var.set(engagement)
        try:
            entered = await client.__aenter__()
            self._clients[engagement.id] = entered or client
            self._ctx_stack[engagement.id] = client
            self._locks[engagement.id] = asyncio.Lock()
            logger.info(
                "Engagement %s resumed (session=%s)",
                engagement.id[:8], session_id,
            )
        finally:
            engagement_var.reset(token)

    def get_session_id(self, engagement: EngagementRecord) -> str | None:
        """Return the session id captured from this engagement's message stream
        (v0.69.11), falling back to the record's persisted value. The live
        ``ClaudeSDKClient`` has no ``session_id`` attribute (pinned SDK), so the
        stream-sourced value is authoritative."""
        return (
            self._session_ids.get(engagement.id)
            or getattr(engagement, "sdk_session_id", None)
        )

    def is_alive(self, engagement: EngagementRecord) -> bool:
        return engagement.id in self._clients

    # -- internal ---------------------------------------------------------

    async def _deliver_turn(
        self, engagement: EngagementRecord, prompt: str,
    ) -> None:
        # Lazy import: tools imports engagement_registry; doing this at
        # module top-level would create a circular import.
        from tools import engagement_var

        client = self._clients[engagement.id]
        lock = self._locks[engagement.id]
        assert engagement.topic_id is not None
        # Phase 3b: stream per-AssistantMessage rather than buffer the
        # entire turn.
        stream = self._topic_stream_factory(engagement.topic_id)
        accumulated = ""
        # Task 6 (spec §4.6): per-turn output bound for INTERACTIVE SPECIALIST
        # engagements — the streamed assistant text was otherwise unbounded
        # before emit_completion. Once the accumulator crosses the cap it is
        # frozen with a marker and further assistant text is skipped (the
        # stream still drains). Executor engagements are out of scope.
        cap_output = getattr(engagement, "kind", "") == "specialist"
        stream_truncated = False
        idx = 0  # Phase 4b: per-turn AssistantMessage counter.
        started_ms = time.monotonic() * 1000  # Phase 4b: turn duration anchor.
        # Per-call tool name lookup so log_tool_result can render name=.
        tool_names_by_id: dict[str, str] = {}
        token = engagement_var.set(engagement)
        try:
            async with lock:
                await client.query(prompt)
                async for sdk_msg in client.receive_response():
                    sid = _session_id_from_message(sdk_msg)
                    if sid:
                        self._session_ids[engagement.id] = sid
                    if (
                        sid
                        and self._persist_session_id is not None
                        and engagement.sdk_session_id != sid
                    ):
                        try:
                            await self._persist_session_id(engagement.id, sid)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "Engagement %s persist_session_id failed: %s",
                                engagement.id[:8], exc,
                            )
                        engagement.sdk_session_id = sid
                    # Phase 4b dispatch — wrapped in try/except so a
                    # malformed block does not abort the rest of the turn.
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
                                    sdk_logging.log_tool_use(
                                        block,
                                        idx=idx,
                                        started_ms=started_ms,
                                    )
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
                            # Task 6 (spec §4.6): feed interactive specialist
                            # cost/usage to the telemetry observer. Guarded —
                            # an observability hook must never abort the turn.
                            if self._result_observer is not None:
                                try:
                                    self._result_observer(engagement, sdk_msg)
                                except Exception:  # noqa: BLE001
                                    logger.warning(
                                        "result_observer raised for engagement %s",
                                        engagement.id[:8], exc_info=True,
                                    )
                    except Exception as dispatch_exc:  # noqa: BLE001
                        logger.warning(
                            "phase4b dispatch failed: %s", dispatch_exc,
                            exc_info=True,
                        )
                    # Phase 3b streaming — Task 6 output bound for specialists.
                    if isinstance(sdk_msg, AssistantMessage) and not stream_truncated:
                        msg_text = "".join(
                            b.text for b in getattr(sdk_msg, "content", [])
                            if isinstance(b, TextBlock)
                        )
                        if msg_text:
                            candidate = (
                                f"{accumulated}\n\n{msg_text}"
                                if accumulated else msg_text
                            )
                            import specialist_limits
                            cap = specialist_limits._MAX_OUTPUT_CHARS
                            if cap_output and len(candidate) > cap:
                                accumulated = candidate[:cap] + " … [truncated]"
                                stream_truncated = True
                                # Persist a flag so the finalize/notification
                                # path can disclose the clipped stream.
                                engagement.origin["stream_output_truncated"] = True
                                logger.warning(
                                    "engagement %s specialist stream output "
                                    "truncated at %d chars (spec §4.6)",
                                    engagement.id[:8], cap,
                                )
                            else:
                                accumulated = candidate
                            await stream.emit(accumulated)
        finally:
            engagement_var.reset(token)
        final = accumulated.strip()
        if final:
            await stream.finalize(final)

        # G-4 (v0.33.0): exploration2 found a configurator engagement
        # that finalized outcome=error 24s after system_init with zero
        # tool_uses inside the subprocess and no log evidence of why.
        # When the SDK loop completes without producing any
        # AssistantMessage frames (idx never incremented), surface the
        # empty turn as a structured warning so operators have a
        # starting signal. Causes include: hook payload synthesis denial
        # (G-1 class), model refusal at system-prompt time, or
        # subprocess crash between init and first message.
        if idx == 0:
            logger.warning(
                "Engagement %s subprocess_terminated "
                "reason=no_assistant_message",
                engagement.id[:8],
            )
