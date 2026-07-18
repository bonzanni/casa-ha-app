"""VoiceChannel — dual-ingress (SSE + WS) channel for the butler agent.

Unlike Telegram, VoiceChannel does not own an IO loop. It mounts HTTP
routes on the aiohttp app already created by casa_core.py. The Channel
start()/stop() hooks exist for lifecycle (sweeper task), not transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from aiohttp import web

from agent import _classify_error
from bus import BusMessage, MessageBus, MessageType
from channel_authz import agent_allowed_on
from channels import Channel
from log_cid import new_cid
from provenance import sanitize_external_context
from rate_limit import RateLimiter
from channels.voice.catalog import (
    VOICE_AGENT_CATALOG_PATH,
    VoiceAgentCatalogError,
    build_voice_agent_catalog,
)
from channels.voice.prosodic import ProsodicSplitter
from channels.voice.routes import VoiceRouteRegistry, VoiceWsConnection
from channels.voice.session import VoiceSessionPool
from channels.voice.tts_adapter import TagDialectAdapter
from job_registry import JobTransitionError, VoiceJob
from semantic_memory import SemanticMemory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceHandoff:
    """The sole foreground frame data produced from a durable voice job."""

    utterance_id: str
    handoff_id: str
    text: str

    @classmethod
    def from_job(cls, utterance_id: str, job: VoiceJob) -> "VoiceHandoff":
        """Select the fixed acknowledgement without exposing job content."""
        return cls(
            utterance_id=utterance_id,
            handoff_id=job.handoff_id or "",
            text=f"I will ask {job.specialist_display_name}.",
        )

    def frame(self) -> dict[str, str]:
        return {
            "type": "handoff",
            "utterance_id": self.utterance_id,
            "handoff_id": self.handoff_id,
            "text": self.text,
        }


class VoiceHandoffReservation:
    """Request-local foreground ownership for one potential WS handoff.

    The reservation has no task, context, routing, or user data.  Tools can
    synchronously reserve/release it while the channel owns the one-way commit
    callback that resolves the foreground handoff future after durability.
    """

    def __init__(self) -> None:
        self._held = False
        self._speech_sent = False
        self._committed = False
        self._commit_callback: Callable[[VoiceJob], None] | None = None

    @property
    def held(self) -> bool:
        return self._held

    def bind_commit(self, callback: Callable[[VoiceJob], None]) -> None:
        """Install the channel-owned completion callback exactly once."""
        if self._commit_callback is not None:
            raise RuntimeError("voice handoff commit callback already bound")
        self._commit_callback = callback

    def reserve(self) -> bool:
        """Suppress foreground output, unless this turn has already spoken."""
        if self._speech_sent:
            return False
        self._held = True
        return True

    def release(self) -> None:
        """Let a typed prelaunch failure resume the ordinary response turn."""
        if not self._committed:
            self._held = False

    def mark_speech_sent(self) -> None:
        """Close the handoff path once a real speech block reached the wire."""
        self._speech_sent = True

    def commit(self, job: VoiceJob) -> None:
        """Resolve the foreground owner once Task 3 made the job durable."""
        if self._committed:
            return
        callback = self._commit_callback
        if callback is None:
            raise RuntimeError("voice handoff commit callback is not bound")
        callback(job)
        self._committed = True


class VoiceHandoffCoordinator:
    """Send and acknowledge durable handoffs on authenticated routes only."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    @staticmethod
    def _frame(job: VoiceJob) -> dict[str, Any]:
        """Build the intentionally metadata-only coordinator frame."""
        return {
            "type": "voice_handoff",
            "protocol": 2,
            "job_id": job.id,
            "handoff_id": job.handoff_id,
            "specialist_display_name": job.specialist_display_name,
        }

    async def route_connected(self, route: Any) -> None:
        """Reoffer only this route's persisted pending acknowledgements."""
        route_id = _nonempty_identifier(getattr(route, "route_id", None))
        if route_id is None:
            return
        for job in self._registry.pending_handoffs_for_route(route_id):
            await route.send_json(self._frame(job))

    async def handle(self, route: Any, frame: Mapping[str, Any]) -> None:
        """Accept a receipt only for the server-bound route that owns it."""
        if (
            frame.get("type") != "handoff_received"
            or frame.get("protocol") != 2
        ):
            return
        job_id = _nonempty_identifier(frame.get("job_id"))
        handoff_id = _nonempty_identifier(frame.get("handoff_id"))
        route_id = _nonempty_identifier(getattr(route, "route_id", None))
        if job_id is None or handoff_id is None or route_id is None:
            return
        job = self._registry.get(job_id)
        if job is None or job.origin_route_id != route_id:
            return
        try:
            await self._registry.acknowledge_handoff(job_id, handoff_id)
        except JobTransitionError:
            # A duplicate receipt is idempotent in the registry; mismatched
            # IDs and stale lifecycle rows are intentionally silent here.
            return


def _nonempty_identifier(value: Any) -> str | None:
    """Normalize one trusted voice identifier without logging its value."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 512:
        return None
    return normalized


def _connection_voice_route(
    connection: Any,
) -> tuple[str | None, frozenset[str], str | None]:
    """Read route identity only from the server-owned WS connection object.

    Task 4 replaces the raw aiohttp socket with ``VoiceWsConnection``.  The
    two accepted attribute spellings keep this trust seam compatible with
    that wrapper while direct Task-3 tests can use a minimal connection
    double.  Utterance/context fields are intentionally never consulted.
    """
    route_id = _nonempty_identifier(
        getattr(connection, "voice_route_id", None)
    ) or _nonempty_identifier(getattr(connection, "route_id", None))
    raw_capabilities = getattr(
        connection,
        "voice_route_capabilities",
        getattr(
            connection,
            "accepted_capabilities",
            getattr(connection, "capabilities", ()),
        ),
    )
    if not isinstance(raw_capabilities, (set, frozenset, list, tuple)):
        return route_id, frozenset(), _nonempty_identifier(
            getattr(connection, "voice_job_control_id", None),
        )
    capabilities = frozenset(
        item for item in raw_capabilities
        if isinstance(item, str) and item
    )
    return (
        route_id,
        capabilities,
        _nonempty_identifier(
            getattr(connection, "voice_job_control_id", None),
        ),
    )

_DEFAULT_ERROR_LINES = {
    "timeout":       "[flat] That took too long.",
    "rate_limit":    "[flat] I'm busy — try again shortly.",
    "sdk_error":     "[flat] I couldn't reach my brain.",
    "memory_error":  "",
    "channel_error": "[flat] Something went wrong.",
    "unknown":       "[flat] Sorry, something went wrong.",
    # S-1 (2026-07-15, cid 93f501bb): a turn that completes with ZERO spoken
    # output (e.g. max_turns exhausted on ToolSearch round-trips) must never
    # end as a bare `done` — a voice user just hears silence.
    "empty_turn":    "[apologetic] Sorry, I lost my train of thought — "
                     "could you ask that again?",
}

# A4 (spec A4): voice turn-budget envelope. INTEGRATION_TIMEOUT_TOTAL is
# the total wall-clock time the voice transport (SSE/WS plus any fronting
# proxy) gives one turn before ITS OWN timeout would fire — a synchronous
# specialist delegation must always leave that much room. The hard cap
# holds even if INTEGRATION_TIMEOUT_TOTAL is raised later.
INTEGRATION_TIMEOUT_TOTAL: float = 30.0
_VOICE_TURN_BUDGET_HARD_CAP_S: float = 27.0
_VOICE_TURN_BUDGET_MIN_S: float = 10.0


def _voice_turn_budget_s() -> float:
    """Effective per-turn delegation budget (spec A4).

    ``min(voice_turn_budget_seconds, INTEGRATION_TIMEOUT_TOTAL - 3)``,
    configured via the ``VOICE_TURN_BUDGET_SECONDS`` env var (default 27),
    clamped to the add-on schema's ``[10, 27]`` rail regardless of
    configuration (defence in depth — HA schema-validates normal config, but
    a direct env override or schema drift must not slip a sub-10s budget
    past, which would starve every delegation).

    A non-finite configured value (``nan``/``inf``) is REJECTED and falls
    back to 27 — a NaN budget would propagate through ``min()`` and defeat
    the deadline entirely (``asyncio.wait(timeout=nan)`` never reliably
    expires), so it must fail closed here at the source.
    """
    try:
        configured = float(os.environ.get("VOICE_TURN_BUDGET_SECONDS", "27"))
    except (TypeError, ValueError):
        configured = 27.0
    if not math.isfinite(configured):
        configured = 27.0
    # Floor at the schema minimum, then apply the transport/hard-cap ceilings.
    configured = max(configured, _VOICE_TURN_BUDGET_MIN_S)
    budget = min(configured, INTEGRATION_TIMEOUT_TOTAL - 3.0)
    return min(budget, _VOICE_TURN_BUDGET_HARD_CAP_S)


class VoiceChannel(Channel):
    name: str = "voice"

    def __init__(
        self,
        bus: MessageBus,
        default_agent: str,
        webhook_secret: str,
        sse_path: str,
        ws_path: str,
        agent_configs: Mapping[str, Any],
        memory: SemanticMemory,
        idle_timeout: int,
        sse_enabled: bool = True,
        ws_enabled: bool = True,
        rate_limiter: RateLimiter | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        route_registry: VoiceRouteRegistry | None = None,
        delivery_coordinator: Any | None = None,
        handoff_coordinator: VoiceHandoffCoordinator | None = None,
    ) -> None:
        self._bus = bus
        self.default_agent = default_agent
        self._webhook_secret = webhook_secret
        self._sse_path = sse_path
        self._ws_path = ws_path
        self._sse_enabled = sse_enabled
        self._ws_enabled = ws_enabled
        self._agent_configs = agent_configs
        self._memory = memory
        self.pool = VoiceSessionPool(idle_timeout=idle_timeout)
        self._sweeper: asyncio.Task | None = None
        # Per-scope_id rate limit (spec 5.2 §8). None = unlimited.
        self._rate_limiter = rate_limiter
        self._monotonic = monotonic
        self.routes = route_registry or VoiceRouteRegistry(
            secret_present=bool(webhook_secret),
            agent_configs=agent_configs,
        )
        self._delivery = delivery_coordinator
        self._handoff = handoff_coordinator

    # --- Channel ABC --------------------------------------------------

    async def start(self) -> None:
        if self._delivery is not None:
            await self._delivery.start()
        self._sweeper = asyncio.create_task(self.pool.run_sweeper())
        logger.info(
            "Voice channel active (sse=%s, ws=%s, sse_path=%s, ws_path=%s)",
            self._sse_enabled, self._ws_enabled, self._sse_path, self._ws_path,
        )

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
            self._sweeper = None
        if self._delivery is not None:
            await self._delivery.stop()

    async def send(self, message: str, context: dict) -> None:
        # Voice has no out-of-band send path — responses are delivered
        # inline on the request's transport. No-op for the ChannelManager's
        # outbound registration (kept so the Channel ABC is satisfied).
        return None

    # --- create_on_token: adapter for the production Agent path -------

    def create_on_token(self, context: dict) -> Callable[[str], Awaitable[None]]:
        """Return the per-utterance streaming callback stored in context.

        The SSE/WS handler stashes the real callback in
        ``context["_on_token"]`` before dispatching on the bus. The
        production ``Agent._process`` resolves it via
        ``channel_manager.get("voice").create_on_token(msg.context)`` —
        so both the test stub (which reads context directly) and the
        production agent (which goes through this method) converge on
        the same callable.
        """
        cb = context.get("_on_token")
        if cb is None:
            async def _noop(_text: str) -> None:
                return None
            return _noop
        return cb

    # --- Route registration -------------------------------------------

    def register_routes(self, app: web.Application) -> None:
        if not (self._sse_enabled or self._ws_enabled):
            return
        app.router.add_get(
            VOICE_AGENT_CATALOG_PATH,
            self._voice_agent_catalog_handler,
        )
        if self._sse_enabled:
            app.router.add_post(self._sse_path, self._sse_handler)
        if self._ws_enabled:
            app.router.add_get(self._ws_path, self._ws_handler)

    async def _voice_agent_catalog_handler(
        self,
        request: web.Request,
    ) -> web.Response:
        signature = request.headers.get("X-Webhook-Signature", "")
        if (
            not self._webhook_secret
            or not signature.isascii()
            or not self._verify(request, b"")
        ):
            return web.json_response(
                {"error": "invalid signature"}, status=401,
            )
        try:
            payload = build_voice_agent_catalog(self._agent_configs)
        except VoiceAgentCatalogError as err:
            logger.error(
                "Voice agent catalog unavailable reason=%s", err.args[0],
            )
            return web.json_response(
                {"error": "voice catalog unavailable"},
                status=503,
            )
        return web.json_response(
            payload,
            headers={"Cache-Control": "no-store"},
        )

    # --- HMAC ---------------------------------------------------------

    def _verify(self, request: web.Request, body: bytes) -> bool:
        if not self._webhook_secret:
            return True
        sig = request.headers.get("X-Webhook-Signature", "")
        expected = hmac.new(
            self._webhook_secret.encode(), body, hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    # --- SSE ----------------------------------------------------------

    async def _sse_handler(self, request: web.Request) -> web.StreamResponse:
        ingress_started_ms = self._monotonic() * 1000
        # A4: capture the deadline at TRUE ingress — the first line of the
        # handler, BEFORE body-read/HMAC/JSON validation — so those (I/O-
        # bound, potentially slow) steps are counted against the 27s window
        # rather than silently extending it past HA's ~30s transport
        # timeout. Monotonic (loop.time()).
        voice_deadline = asyncio.get_running_loop().time() + _voice_turn_budget_s()

        body = await request.read()
        if not self._verify(request, body):
            return web.json_response({"error": "invalid signature"}, status=401)
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        prompt = payload.get("prompt") or ""
        if not prompt:
            return web.json_response({"error": "missing 'prompt'"}, status=400)

        agent_role = payload.get("agent_role", self.default_agent)
        cfg = self._agent_configs.get(agent_role)
        # Fail-closed channel-capability gate (spec A3): unknown role and a
        # role that never declared ha_voice get the SAME 404 body — no
        # existence oracle for residents that exist but aren't voice-reachable.
        if cfg is None or not agent_allowed_on("voice", cfg):
            return web.json_response({"error": "unknown agent_role"}, status=404)

        scope_id = self._resolve_scope_id(payload)
        self.pool.ensure(scope_id, role=agent_role)
        self.pool.touch(scope_id, role=agent_role)

        # Rate limit BEFORE opening the SSE stream (spec 5.2 §8).
        if self._rate_limiter is not None and self._rate_limiter.enabled:
            decision = self._rate_limiter.check((agent_role, scope_id))
            if not decision.allowed:
                logger.info(
                    "Voice SSE rate limit hit for role=%s scope_id=%s",
                    agent_role, scope_id,
                )
                response = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
                await response.prepare(request)
                adapter = TagDialectAdapter(cfg.tts.tag_dialect)
                line = VoiceChannel._error_line_for_kind(cfg, "rate_limit")
                await _write_sse(response, "error", {
                    "kind": "rate_limit",
                    "spoken": adapter.render(line) if line else "",
                })
                return response

        utterance_id = str(uuid.uuid4())

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        splitter = ProsodicSplitter()
        adapter = TagDialectAdapter(cfg.tts.tag_dialect)
        last_text = ""
        # A4: write_lock serializes real SDK-streamed blocks (on_token)
        # against the synthetic progress block (_progress_sink below). The
        # flag CHECK, the wire WRITE, and the flag MUTATION all happen under
        # the SAME held lock in both closures, so there is no window where
        # the progress sink can observe speech_block_sent=False, queue
        # behind an in-flight on_token write, and then emit progress AFTER
        # real speech. speech_block_sent flips ONLY after a real block is
        # actually written (not on any token/last_text update); progress_sent
        # flips ONLY after the progress block is actually written.
        write_lock = asyncio.Lock()
        speech_block_sent = False
        progress_sent = False
        first_block_logged = False

        def _log_first_block() -> None:
            nonlocal first_block_logged
            if first_block_logged:
                return
            logger.info(
                "voice_first_block role=%s transport=sse ms=%d",
                agent_role,
                int(self._monotonic() * 1000 - ingress_started_ms),
            )
            first_block_logged = True

        async def on_token(accumulated: str) -> None:
            nonlocal last_text, splitter, speech_block_sent
            if not accumulated.startswith(last_text):
                # AR-B (2026-07-11 design §2 point 3): a mid-turn SDK retry
                # or a divergent canonical correction breaks the
                # "accumulated always extends last_text" assumption the
                # delta slice below depends on. Reset to a fresh splitter
                # so the new cumulative re-renders cleanly from its own
                # start instead of computing a bogus delta (mid-word
                # garbage, or a silently swallowed restart).
                logger.debug(
                    "voice sse on_token non-prefix cumulative "
                    "(len=%d vs last_text len=%d); resetting splitter "
                    "scope_id=%s",
                    len(accumulated), len(last_text), scope_id,
                )
                last_text = ""
                splitter = ProsodicSplitter()
            delta = accumulated[len(last_text):]
            last_text = accumulated
            for block in splitter.feed(delta):
                async with write_lock:
                    await _write_sse(response, "block", {
                        "text": adapter.render(block),
                        "final": False,
                    })
                    _log_first_block()
                    speech_block_sent = True

        async def _progress_sink(text: str) -> None:
            # A4: deterministic "still working" block for a mid-turn
            # specialist delegation. Exactly once per outer voice turn
            # (progress_sent) and suppressed once the turn has spoken any
            # REAL content (speech_block_sent). The check + write + mutation
            # are ALL under the lock — see the write_lock comment above.
            # Writes a real wire `block` — NOT via on_token, whose
            # cumulative-prefix `last_text` bookkeeping would corrupt on a
            # manually-injected block not part of the accumulated SDK text.
            nonlocal progress_sent
            async with write_lock:
                if progress_sent or speech_block_sent:
                    return
                await _write_sse(response, "block", {
                    "text": adapter.render(text), "final": False,
                })
                progress_sent = True

        error_emitted = False

        async def _error_sink(kind: str, spoken: str) -> None:
            nonlocal error_emitted
            await _write_sse(response, "error", {
                "kind": kind, "spoken": spoken,
            })
            error_emitted = True

        external_context = sanitize_external_context(payload.get("context"))
        msg = BusMessage(
            type=MessageType.REQUEST,
            source="voice",
            target=agent_role,
            content=prompt,
            channel="voice",
            context={
                # Sanitize-and-preserve (A:§3.5): payload["context"] is
                # caller-supplied (the SSE POST body) — strip Casa-reserved
                # provenance keys before Casa's own keys are merged in below.
                **external_context,
                "chat_id": scope_id,
                "utterance_id": utterance_id,
                "cid": request["cid"],
                "_on_token": on_token,
                "_error_sink": _error_sink,
                "_voice_deadline": voice_deadline,
                "_progress_sink": _progress_sink,
                # SSE can complete only the live request. It never advertises
                # an out-of-band delivery route, even when its external
                # context contains route-shaped spoof fields.
                "_voice_transport": "sse",
            },
        )

        try:
            result = await self._bus.request(msg, timeout=300)
            if error_emitted:
                return response
            tail = splitter.flush_tail()
            if tail:
                await _write_sse(response, "block", {
                    "text": adapter.render(tail),
                    "final": True,
                })
                _log_first_block()
            elif not speech_block_sent:
                # S-1: zero spoken output for the whole turn — emit a typed
                # empty_turn error line instead of a silent bare `done`
                # (mirrors every other error path: error frame, no done).
                line = self._error_line_for_kind(cfg, "empty_turn")
                await _write_sse(response, "error", {
                    "kind": "empty_turn",
                    "spoken": adapter.render(line) if line else "",
                })
                return response
            await _write_sse(response, "done", {})
        except asyncio.CancelledError:
            # Client disconnect mid-stream — do NOT emit `event: done`.
            # Pool entry already created above, stays alive per spec §10.3.
            raise
        except Exception as exc:
            line = self._error_line(cfg, exc)
            await _write_sse(response, "error", {
                "kind": _classify_error(exc).value,
                "spoken": adapter.render(line) if line else "",
            })

        return response

    # --- helpers ------------------------------------------------------

    @staticmethod
    def _resolve_scope_id(payload: dict) -> str:
        if payload.get("scope_id"):
            return payload["scope_id"]
        ctx = payload.get("context") or {}
        return (
            ctx.get("user_id")
            or ctx.get("device_id")
            or ctx.get("conversation_id")
            or "anon"
        )

    @staticmethod
    def _error_line(cfg: Any, exc: Exception) -> str:
        kind = _classify_error(exc).value
        return VoiceChannel._error_line_for_kind(cfg, kind)

    @staticmethod
    def _error_line_for_kind(cfg: Any, kind: str) -> str:
        lines = getattr(cfg, "voice_errors", {}) or {}
        return lines.get(kind) or _DEFAULT_ERROR_LINES.get(kind, "")

    async def emit_error_line(
        self, kind: str, context: dict, agent_cfg: Any,
    ) -> bool:
        """Emit a persona-voice error line via the per-request sink.

        Called by Agent.handle_message's error branch. Returns True if
        the error was delivered to the client (caller should suppress
        normal text delivery). Returns False if no sink is present
        (e.g. this was called outside a live SSE/WS request).
        """
        sink = context.get("_error_sink")
        if sink is None:
            return False
        line = VoiceChannel._error_line_for_kind(agent_cfg, kind)
        adapter = TagDialectAdapter(agent_cfg.tts.tag_dialect)
        spoken = adapter.render(line) if line else ""
        await sink(kind, spoken)
        return True

    # --- WS ------------------------------------------------------------

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        if not self._verify(request, b""):
            return web.json_response({"error": "invalid signature"}, status=401)

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connection = VoiceWsConnection(ws)

        # Per-utterance task map so `cancel` frames can target them.
        tasks: dict[str, asyncio.Task] = {}

        try:
            async for msg in ws:
                if msg.type.name != "TEXT":
                    continue
                try:
                    frame = json.loads(msg.data)
                except Exception:
                    continue
                if not isinstance(frame, dict):
                    continue
                t = frame.get("type")

                self.routes.touch(connection)

                if t == "voice_route_register":
                    bound = await self.routes.register(connection, frame)
                    if bound is not None and self._delivery is not None:
                        await self._delivery.route_connected(bound)
                    if bound is not None and self._handoff is not None:
                        await self._handoff.route_connected(bound)
                    continue

                if isinstance(t, str) and t.startswith("job_"):
                    if self._delivery is not None:
                        await self._delivery.handle(connection, frame)
                    continue

                if t == "handoff_received":
                    route_id = _nonempty_identifier(
                        getattr(connection, "voice_route_id", None)
                    )
                    bound = (
                        self.routes.get_connected(route_id)
                        if route_id is not None else None
                    )
                    if (
                        bound is not None
                        and bound.connection is connection
                        and self._handoff is not None
                    ):
                        await self._handoff.handle(bound, frame)
                    continue

                if t == "stt_start":
                    scope_id = _nonempty_identifier(frame.get("scope_id"))
                    agent_role = _nonempty_identifier(frame.get("agent_role"))
                    cfg = (
                        self._agent_configs.get(agent_role)
                        if agent_role is not None else None
                    )
                    if (
                        scope_id is not None
                        and agent_role is not None
                        and cfg is not None
                        and agent_allowed_on("voice", cfg)
                    ):
                        # Pool metadata only. SDK prewarm remains the separate,
                        # conditional Tina T2 optimization.
                        self.pool.ensure(scope_id, role=agent_role)
                    continue

                if t == "stage":
                    # Hints only — no-op in 2.3.
                    continue

                if t == "cancel":
                    uid = frame.get("utterance_id")
                    task = tasks.get(uid) if uid else None
                    if task is not None and not task.done():
                        task.cancel()
                    continue

                if t == "utterance":
                    uid = frame.get("utterance_id") or str(uuid.uuid4())
                    # Server-owned anchor: overwrite any identically named client
                    # field before handing the frame to the scheduled task.
                    frame["_casa_ingress_started_ms"] = self._monotonic() * 1000
                    # A4: capture the deadline at TRUE ingress — the moment the
                    # utterance frame is RECEIVED here, not inside the separately-
                    # scheduled _run_ws_utterance task (which may not run for an
                    # unbounded interval under load). Monotonic (loop.time()).
                    voice_deadline = (
                        asyncio.get_running_loop().time()
                        + _voice_turn_budget_s()
                    )
                    task = asyncio.create_task(
                        self._run_ws_utterance(
                            connection, frame, uid, voice_deadline,
                        ),
                    )
                    tasks[uid] = task

                    def _reap(
                        done_task: asyncio.Task, uid: str = uid,
                    ) -> None:
                        # Prune only if this entry wasn't overwritten by a
                        # duplicate uid. `done_task` (the callback's own arg)
                        # IS the finished task, so this closure never holds a
                        # separate strong reference to it beyond the callback's
                        # own (transient) invocation.
                        if tasks.get(uid) is done_task:
                            tasks.pop(uid, None)
                        if done_task.cancelled():
                            return
                        # Retrieve so GC never logs 'never retrieved'.
                        exc = done_task.exception()
                        if exc is not None:
                            logger.warning(
                                "Voice WS utterance task failed "
                                "(utterance_id=%s): %s",
                                uid, exc,
                            )

                    task.add_done_callback(_reap)
                    # Drop the frame-local reference so a finished task is not
                    # kept alive by this coroutine's own suspended stack frame
                    # while it awaits the next WS frame.
                    del task
                    continue
        finally:
            # Clear the server-bound writer even when a handler or server
            # shutdown aborts the reader loop.
            pending = [task for task in tasks.values() if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            disconnected = await self.routes.disconnect(connection)
            if disconnected is not None and self._delivery is not None:
                await self._delivery.route_disconnected(disconnected)
        return ws

    async def _run_ws_utterance(
        self, ws: VoiceWsConnection, frame: dict, uid: str,
        voice_deadline: float,
    ) -> None:
        # A4: `voice_deadline` (monotonic loop.time()) is captured by the
        # caller at utterance-frame RECEIPT — see _ws_handler — so any delay
        # between receipt and this task actually running is counted against
        # the budget rather than silently extending it.
        agent_role = frame.get("agent_role", self.default_agent)
        cfg = self._agent_configs.get(agent_role)
        # Fail-closed channel-capability gate (spec A3): a 404 can't follow
        # the WS upgrade, so an unknown role AND a role that never declared
        # ha_voice both get the same `unknown_agent` error frame, emitted
        # BEFORE any bus dispatch.
        if cfg is None or not agent_allowed_on("voice", cfg):
            await ws.send_json({
                "type": "error", "utterance_id": uid,
                "kind": "unknown_agent", "spoken": "",
            })
            return

        scope_id = self._resolve_scope_id({
            "scope_id": frame.get("scope_id"),
            "context": frame.get("context") or {},
        })
        self.pool.ensure(scope_id, role=agent_role)
        self.pool.touch(scope_id, role=agent_role)

        # Rate limit BEFORE dispatching to the agent (spec 5.2 §8).
        if self._rate_limiter is not None and self._rate_limiter.enabled:
            decision = self._rate_limiter.check((agent_role, scope_id))
            if not decision.allowed:
                logger.info(
                    "Voice WS rate limit hit for role=%s scope_id=%s "
                    "utterance_id=%s",
                    agent_role, scope_id, uid,
                )
                adapter = TagDialectAdapter(cfg.tts.tag_dialect)
                line = VoiceChannel._error_line_for_kind(cfg, "rate_limit")
                await ws.send_json({
                    "type": "error", "utterance_id": uid,
                    "kind": "rate_limit",
                    "spoken": adapter.render(line) if line else "",
                })
                return

        splitter = ProsodicSplitter()
        adapter = TagDialectAdapter(cfg.tts.tag_dialect)
        last_text = ""
        error_emitted = False
        # A4: mirrors the SSE handler's write_lock/speech_block_sent —
        # see its on_token for the full rationale.
        write_lock = asyncio.Lock()
        speech_block_sent = False
        progress_sent = False
        handoff: asyncio.Future[VoiceHandoff] = (
            asyncio.get_running_loop().create_future()
        )
        reservation = VoiceHandoffReservation()

        def commit_handoff(job: VoiceJob) -> None:
            if not handoff.done():
                handoff.set_result(VoiceHandoff.from_job(uid, job))

        reservation.bind_commit(commit_handoff)
        first_block_logged = False
        ingress_started_ms = frame.get("_casa_ingress_started_ms")
        if ingress_started_ms is None:
            # Direct internal callers (tests/helpers) bypass _ws_handler.
            ingress_started_ms = self._monotonic() * 1000

        def _log_first_block() -> None:
            nonlocal first_block_logged
            if first_block_logged:
                return
            logger.info(
                "voice_first_block role=%s transport=ws ms=%d",
                agent_role,
                int(self._monotonic() * 1000 - ingress_started_ms),
            )
            first_block_logged = True

        async def on_token(accumulated: str) -> None:
            nonlocal last_text, splitter, speech_block_sent
            async with write_lock:
                # A handoff reserve happens before any async prelaunch gate.
                # Do not mutate prefix state while held: a later typed failure
                # releases the reservation and the next cumulative token can
                # resume normal speech from the previous real prefix.
                if reservation.held:
                    return
                if not accumulated.startswith(last_text):
                    # AR-B — see the SSE handler's on_token for rationale.
                    logger.debug(
                        "voice ws on_token non-prefix cumulative "
                        "(len=%d vs last_text len=%d); resetting splitter "
                        "utterance_id=%s scope_id=%s",
                        len(accumulated), len(last_text), uid, scope_id,
                    )
                    last_text = ""
                    splitter = ProsodicSplitter()
                delta = accumulated[len(last_text):]
                last_text = accumulated
                for block in splitter.feed(delta):
                    await ws.send_json({
                        "type": "block", "utterance_id": uid,
                        "text": adapter.render(block), "final": False,
                    })
                    _log_first_block()
                    speech_block_sent = True
                    reservation.mark_speech_sent()

        async def _progress_sink(text: str) -> None:
            # A4: see the SSE handler's _progress_sink for the full
            # exactly-once / suppress-after-real-speech rationale — the
            # check + write + mutation all happen under the held lock.
            nonlocal progress_sent
            async with write_lock:
                if reservation.held or progress_sent or speech_block_sent:
                    return
                await ws.send_json({
                    "type": "block", "utterance_id": uid,
                    "text": adapter.render(text), "final": False,
                })
                progress_sent = True

        async def _error_sink(kind: str, spoken: str) -> None:
            nonlocal error_emitted
            await ws.send_json({
                "type": "error", "utterance_id": uid,
                "kind": kind, "spoken": spoken,
            })
            error_emitted = True

        external_context = sanitize_external_context(frame.get("context"))
        route_id, route_capabilities, job_control_id = (
            _connection_voice_route(ws)
        )
        # The integration frame is authenticated by the WS route.  Ordinary
        # external context remains available to the agent but must never be
        # promoted into trusted job-delivery provenance.
        origin_device_id = _nonempty_identifier(frame.get("device_id"))
        trusted_route_context: dict[str, Any] = {
            "_voice_transport": "ws",
        }
        if route_id is not None:
            trusted_route_context["_voice_route_id"] = route_id
        if route_capabilities:
            trusted_route_context["_voice_route_capabilities"] = (
                route_capabilities
            )
        if origin_device_id is not None:
            trusted_route_context["_origin_device_id"] = origin_device_id
        if job_control_id is not None:
            trusted_route_context["_voice_job_control_id"] = job_control_id

        bus_msg = BusMessage(
            type=MessageType.REQUEST, source="voice", target=agent_role,
            content=frame.get("text", ""),
            channel="voice",
            context={
                # Sanitize-and-preserve (A:§3.5): frame["context"] is
                # caller-supplied (the WS utterance frame) — strip
                # Casa-reserved provenance keys before Casa's own keys are
                # merged in below.
                **external_context,
                "chat_id": scope_id, "utterance_id": uid,
                "cid": new_cid(),
                "_on_token": on_token,
                "_error_sink": _error_sink,
                "_voice_deadline": voice_deadline,
                "_progress_sink": _progress_sink,
                "_voice_handoff_reservation": reservation,
                **trusted_route_context,
            },
        )

        request_task = asyncio.create_task(
            self._bus.request(bus_msg, timeout=300),
            name=f"voice-request-{uid}",
        )
        try:
            done, _ = await asyncio.wait(
                {request_task, handoff},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if handoff in done:
                try:
                    foreground_handoff = handoff.result()
                    async with write_lock:
                        await ws.send_json(foreground_handoff.frame())
                except BaseException:
                    # The pending latch remains durable for Task 3's reconnect
                    # reoffer.  Tear down only this foreground request before
                    # taking the ordinary connection/error path below.
                    if not request_task.done():
                        request_task.cancel()
                    await asyncio.gather(request_task, return_exceptions=True)
                    raise
                if not request_task.done():
                    request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
                return

            # The normal request won.  Its handler has either released a
            # prelaunch reservation or never reserved it, so retain all prior
            # streaming/done behaviour and consume the unused callback waiter.
            handoff.cancel()
            await asyncio.gather(handoff, return_exceptions=True)
            await request_task
            if error_emitted:
                return
            tail = splitter.flush_tail()
            if tail:
                await ws.send_json({
                    "type": "block", "utterance_id": uid,
                    "text": adapter.render(tail), "final": True,
                })
                _log_first_block()
            elif not speech_block_sent:
                # S-1: zero spoken output — typed empty_turn error, never a
                # silent bare `done`. See the SSE handler for rationale.
                line = self._error_line_for_kind(cfg, "empty_turn")
                await ws.send_json({
                    "type": "error", "utterance_id": uid,
                    "kind": "empty_turn",
                    "spoken": adapter.render(line) if line else "",
                })
                return
            await ws.send_json({"type": "done", "utterance_id": uid})
        except asyncio.CancelledError:
            # Cancellation from a `cancel` frame — drop partial state; do
            # not emit `done`. Pool stays alive per spec §10.3.
            if not request_task.done():
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
            if not handoff.done():
                handoff.cancel()
            raise
        except Exception as exc:
            line = self._error_line(cfg, exc)
            await ws.send_json({
                "type": "error", "utterance_id": uid,
                "kind": _classify_error(exc).value,
                "spoken": adapter.render(line) if line else "",
            })

async def _write_sse(response: web.StreamResponse, event: str, data: dict) -> None:
    payload = (
        f"event: {event}\n"
        f"data: {json.dumps(data)}\n\n"
    )
    await response.write(payload.encode("utf-8"))
